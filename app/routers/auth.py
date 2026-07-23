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

# route: /setup
@router.get("/setup")
async def setup_page(request: Request, db: Session = Depends(get_db)):
    if not ctx._needs_setup(db):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    error = request.query_params.get("error")
    return ctx.templates.TemplateResponse(request, "setup.html", {"error": error or None})



# route: /setup
@router.post("/setup")
async def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    if not username or not password:
        return ctx.templates.TemplateResponse(
            request, "setup.html",
            {"error": "Username and password are required"},
        )

    # Serialize first-user creation (SQLite IMMEDIATE write lock + re-check)
    if db.in_transaction():
        db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        if db.query(User).count() > 0:
            db.rollback()
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

        user = User(username=username, hashed_password=hash_password(password))
        db.add(user)
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise

    ctx._audit_log(db, request, "setup.user_created", user_id=user.id,
               resource_type="user", resource_id=user.id, details={"username": username})

    try:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return ctx._set_session_cookies(response, request, user.username)
    except ValueError:
        return ctx.templates.TemplateResponse(
            request, "setup.html",
            {"error": "Account created, but PARA_SCOPE_SECRET_KEY is required to sign in. Set it and log in."},
        )



# route: /login
@router.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    if ctx._needs_setup(db):
        return RedirectResponse(url="/setup", status_code=status.HTTP_303_SEE_OTHER)
    user = ctx._get_user(request, db)
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    error = request.query_params.get("error")
    return ctx.templates.TemplateResponse(request, "login.html", {"error": error or None})



# route: /login
@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if ctx._needs_setup(db):
        return RedirectResponse(url="/setup", status_code=status.HTTP_303_SEE_OTHER)
    ip = request.client.host if request.client else "unknown"
    if not ctx._check_login_rate_limit(ip):
        ctx._audit_log(db, request, "login_rate_limited", details={"ip": ip})
        return ctx.templates.TemplateResponse(request, "login.html", {"error": "Too many login attempts. Try again later."})
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        ctx._audit_log(db, request, "login_failure", details={"username": username})
        return ctx.templates.TemplateResponse(request, "login.html", {"error": "Invalid credentials"})
    try:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        ctx._set_session_cookies(response, request, user.username)
    except ValueError:
        return ctx.templates.TemplateResponse(
            request, "login.html",
            {"error": "Server misconfigured: PARA_SCOPE_SECRET_KEY is required"},
        )
    ctx._audit_log(db, request, "login_success", user_id=user.id)
    return response



# route: /logout
@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        key="session_username", path="/", samesite="lax", secure=ctx._SECURE_COOKIES,
    )
    response.delete_cookie(
        key="csrf_token", path="/", samesite="lax", secure=ctx._SECURE_COOKIES,
    )
    return response



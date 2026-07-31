"""Para-Scope FastAPI entry — middleware, static, router includes."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.database import engine, Base
from app.scheduler import start_scheduler, stop_scheduler
from app.webctx import (
    AuthMiddleware, CsrfProtectMiddleware, templates, http_logger,
)
from app.routers import auth, dashboard, pipeline, system, webhook

Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import os
    # Tests set PARA_SCOPE_SECRET_KEY in app.tests / pytest env.
    if not os.environ.get("PARA_SCOPE_SECRET_KEY", "").strip():
        raise RuntimeError(
            "PARA_SCOPE_SECRET_KEY is required. Set it in .env "
            "(generate with: openssl rand -hex 32)."
        )
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Para-Scope", version="0.1.0", lifespan=lifespan)

# Last added = outermost: CSRF runs before Auth.
app.add_middleware(AuthMiddleware)
app.add_middleware(CsrfProtectMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(pipeline.router)
app.include_router(system.router)
app.include_router(webhook.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, StarletteHTTPException):
        return await http_exception_handler(request, exc)
    if isinstance(exc, RequestValidationError):
        return await request_validation_exception_handler(request, exc)
    http_logger.exception(
        "Unhandled exception method=%s path=%s", request.method, request.url.path
    )
    return JSONResponse({"error": "Internal server error"}, status_code=500)


# Re-export for tests that patch app.main rate-limit dicts
from app.webctx import (  # noqa: E402
    _LOGIN_RATE_LIMIT,
    _WEBHOOK_REPLAY_CACHE,
    _WEBHOOK_RATE_LIMIT,
)

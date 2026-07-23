# Para-Scope — Agent Instructions

## Run
```
uv venv .venv && uv pip install -r requirements.txt -p .venv
source .venv/bin/activate
cp .env.example .env                   # set PARA_SCOPE_SECRET_KEY
uvicorn app.main:app --reload          # dev server (auto-creates DB on first start)
# First visit → /setup creates the initial user (or: python create_user.py)
```

## Architecture
Single-process FastAPI app. No migrations, no Docker requirement.

| Layer | Files |
|-------|-------|
| Entry | `app/main.py` — app factory, middleware, static mount, router includes |
| Routes | `app/routers/` — `auth`, `dashboard`, `pipeline`, `system`, `webhook` |
| Shared HTTP | `app/webctx.py` — templates, CSRF/auth middleware helpers, form parsers |
| Ingress | `app/ingest.py` — shared event persist + prune used by webhook and pollers |
| Models | `app/models.py` — all SQLAlchemy models (create_all at startup) |
| DB | `app/database.py` — SQLite engine, `PRAGMA foreign_keys=ON`, `ensure_schema()` |
| Auth | `app/security.py` — bcrypt hash/verify; cookie-based session via `session_username` |
| Pipeline | `app/pipeline.py` — rule matching + action dispatch |
| Polling | `app/scheduler.py` + `app/pollers.py` — APScheduler + HTTP poller |
| Widgets | `app/widgets.py` — dashboard widget registry |

Config lives in the DB. Config nav: `/config/pipeline`, `/config/users`, `/config/dashboard`, `/config/style`.

## Gotchas
- **No migrations** — `Base.metadata.create_all()` + `ensure_schema()` run at startup. Add new columns to the model *and* `_SCHEMA_PATCHES` in `database.py` so existing SQLite files get `ALTER TABLE`.
- **SQLite foreign keys are OFF by default** — `database.py` enables them via pragma. Rely on it.
- **Enum class names must not collide with model class names** — `ScheduleType` is the interval/cron enum for `PollingSchedule` (not to be confused with `EventTypeRecord`). Same table name (`event_types`) is fine; only the Python class name matters for relationships.
- **Auth middleware checks `session_username` cookie** — it queries `User` by username. Any change to the User model or session mechanism needs a corresponding middleware update.
- **First-run setup** — with zero users, AuthMiddleware sends browsers to `/setup` (public). After the first user exists, `/setup` redirects to `/login`. Optional CLI: `create_user.py` (needs the DB file from a prior app start).
- **Polling jobs** — registered at startup and on schedule create/delete. Requires `apscheduler` + `httpx`.

## CSS (STYLE.md)
Vanilla CSS, 37signals/Fizzy system. No build step. Files: `app/static/css/`.
- Use `@layer` cascade layers in `main.css`: reset → base → layout → components → utilities.
- OKLCH color tokens (`--lch-gray-*`, `--color-link`, etc.) with automatic dark mode via `prefers-color-scheme`.
- Custom properties as component APIs (e.g., `--btn-background`). Variants = one-line overrides.
- Naming: BEM-inspired (`.card`, `.card__header`, `.card--featured`). Flat file structure, one concept per file.

## Adding a new model
1. Define the class in `app/models.py` with `Base` inheritance.
2. Add `relationship()` references from existing models if needed.
3. Restart — tables are created automatically.

## DESIGN.md
Full design doc at root. Key entities: Source, EventTypeRecord, PollingSchedule, Rule, ActionInstance, Event, MetricPoint, AuditLog, DashboardLayout. Non-goals include multi-region HA, complex workflow engine, and Docker-first deployment.

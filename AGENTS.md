# Para-Scope — Agent Instructions

## Run
```
uv venv .venv && uv pip install -r requirements.txt -p .venv
source .venv/bin/activate
cp .env.example .env                   # set PARA_SCOPE_SECRET_KEY (openssl rand -hex 32)
uvicorn app.main:app --reload          # dev server (auto-creates DB on first start)
# First visit → /setup creates the initial user (or: python create_user.py)
```

## Tests
```
pytest app/tests/ -v
```
Tests use a real SQLite DB (`.test_db.sqlite`) — remove it between runs if schema changed. `PARA_SCOPE_SECRET_KEY` defaults to `"test-secret-key-for-pytest"` in test env. Tests need the venv active; no extra services required.

## Architecture
Single-process FastAPI app. No migrations, no Docker. Everything is DB-backed config with HTMX-driven UI.

| Layer | Files |
|-------|-------|
| Entry | `app/main.py` — app factory, middleware, static mount, router includes |
| Routes | `app/routers/` — `auth`, `dashboard`, `pipeline`, `system`, `webhook` |
| Shared HTTP | `app/webctx.py` — Jinja2 templates, CSRF/auth middleware helpers, form parsers |
| Ingress | `app/ingest.py` — shared event persist + prune used by webhook and pollers |
| Models | `app/models.py` — all SQLAlchemy models (create_all at startup) |
| DB | `app/database.py` — SQLite engine, `PRAGMA foreign_keys=ON`, `create_all` at startup |
| Auth | `app/security.py` — bcrypt hash/verify; signed timed session via `session_username` cookie (itsdangerous URLSafeTimedSerializer) |
| Pipeline | `app/pipeline.py` — rule matching + action dispatch (`evaluate_and_dispatch`) |
| Polling | `app/scheduler.py` + `app/pollers.py` — APScheduler jobs + HTTP poller |
| Widgets | `app/widgets.py` — dashboard widget registry; ApexCharts in `static/vendor/apexcharts/` + `widget-charts.js`; path/maths helpers in `widget_transforms.py` |
| Fields | `app/fields.py` — shared logbook / value / text / toggle state |
| Actions | `app/actions.py` — action type dispatch (field_push, http_forward, notify, web_push, local_script) |

Config lives in the DB. Config nav: `/config/pipeline`, `/config/users`, `/config/dashboard`, `/config/style`.

## Gotchas
- **No migrations** — schema is models + `Base.metadata.create_all()` at startup. Wipe `para_scope.db` / `.test_db.sqlite` when the model changes.
- **SQLite foreign keys are OFF by default** — `database.py` enables them via pragma. Rely on it.
- **Enum class names must not collide with model class names** — `ScheduleType` is the interval/cron enum for `PollingSchedule` (not to be confused with `EventTypeRecord`). Same table name (`event_types`) is fine; only the Python class name matters for relationships.
- **Auth middleware checks `session_username` cookie** — it queries `User` by username and verifies the itsdangerous signature. Any change to the User model or session mechanism needs a corresponding middleware update.
- **First-run setup** — with zero users, AuthMiddleware sends browsers to `/setup` (public). After the first user exists, `/setup` redirects to `/login`. Optional CLI: `create_user.py` (needs the DB file from a prior app start).
- **Polling jobs** — registered at startup and on poll source create/edit/delete. Requires `apscheduler` + `httpx`. Jobs run in-process; never scale beyond 1 worker.
- **Rate-limit dicts live on `app.main`** — tests clear them via `main_mod._LOGIN_RATE_LIMIT.clear()` etc. If you add rate limiting elsewhere, export the dict from `app.main` for test access (same pattern).
- **CSRF** — form POSTs need a `csrf_token` cookie + `_csrf_token` form field; JSON POSTs need `X-CSRF-Token` header. Webhook endpoints skip CSRF entirely.

## Adding a new model
1. Define the class in `app/models.py` with `Base` inheritance.
2. Restart (or wipe the DB file if the schema changed) — tables are created automatically.

## CSS (STYLE.md)
Vanilla CSS, 37signals/Fizzy system. No build step. Files: `app/static/css/`.
- Use `@layer` cascade layers in `main.css`: reset → base → layout → components → utilities.
- OKLCH color tokens (`--lch-gray-*`, `--color-link`, etc.) with automatic dark mode via `prefers-color-scheme`.
- Custom properties as component APIs (e.g., `--btn-background`). Variants = one-line overrides.
- Naming: BEM-inspired (`.card`, `.card__header`, `.card--featured`). Flat file structure, one concept per file.

## DESIGN.md
Full design doc at root. Key entities: Source, EventTypeRecord, PollingSchedule, Rule, ActionInstance, Event, MetricPoint, AuditLog, DashboardLayout. Non-goals include multi-region HA, complex workflow engine, and Docker-first deployment.

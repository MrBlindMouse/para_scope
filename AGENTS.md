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

## Principles

- **YAGNI** — extend existing registries and helpers; do not invent frameworks, plugin systems, or parallel mechanisms for one job.
- **Reuse before rewrite** — `register_action`, `register_poller`, `KIND_DISPLAYS` / widget bindings, `fields.get_by_path`, `widget_transforms` templates, `ingest_event`, webctx form parsers.
- **No new dependencies** if stdlib or an already-installed package covers it.
- **Deletion over abstraction** — shortest working diff; boring over clever; fewest files.
- **Single worker** — rate limits, replay cache, scheduler, and token caches are in-process. Never assume multi-worker.
- **SQLite only / no migrations / no Docker-first** — PostgreSQL and migration tooling are not planned; schema is models + `create_all`; ops are systemd + nginx (see README).
- **Keep auth simple** — retain password + signed-cookie sessions and full access for authenticated users; no roles, API tokens, TOTP, WebAuthn, or OIDC.
- **Keep rules stateless** — conditions match event payloads; do not add per-rule rate limits or time windows.
- **Keep execution in-process** — no durable queue, dead-letter system, or delivery-guarantee framework.
- **Keep extensions built-in** — use `register_action` / `register_poller`; no plugin discovery or Python entry-point loading.
- **Clean breaks over legacy** — prefer wipe/recreate and breaking changes over compatibility shims, dual-path code, or migration frameworks (schema, config shape, APIs). When a change is breaking, **warn the user clearly**: what breaks, what to wipe or reconfigure (`para_scope.db`, `.test_db.sqlite`, env, dashboard layout, etc.).

## Versioning

- **Source of truth:** `app/main.py` → `FastAPI(..., version="X.Y.Z")`. Keep [CHANGELOG.md](CHANGELOG.md) and [DESIGN.md](DESIGN.md) in sync when cutting a release.
- **Workflow:** develop on `main` (short-lived feature branches OK). Cut a release by bumping the version string, updating CHANGELOG, committing, then `git tag vX.Y.Z` and pushing the tag. No long-lived `release/*` branches unless backporting a fix to an old tag.
- **SemVer on `0.x`:** PATCH = no schema/API-shape break; MINOR = additive or (when necessary) documented wipe; breaking changes must lead the CHANGELOG **Breaking** section. Do not invent dual-path compatibility.
- **Before tagging:** `pytest app/tests/ -v` green. Any `models.py` (or session/cookie/template-semantics) change is a wipe warning in CHANGELOG + README upgrade notes.

## Architecture
Single-process FastAPI app. SQLite only, no migrations, no Docker. Everything is DB-backed config with HTMX-driven UI.

| Layer | Files |
|-------|-------|
| Entry | `app/main.py` — app factory, middleware, static mount, router includes |
| Routes | `app/routers/` — `auth`, `dashboard`, `pipeline`, `system`, `webhook` |
| Shared HTTP | `app/webctx.py` — Jinja2 templates, CSRF/auth middleware, form parsers, rate/replay, webhook BG hook |
| Ingress | `app/ingest.py` + `app/event_store.py` — persist + prune (keep `pending`) |
| Models / DB | `app/models.py`, `app/database.py` — all models; SQLite + `PRAGMA foreign_keys=ON` |
| Auth / secrets | `app/security.py` — bcrypt, CSRF mint, timed session, Fernet |
| Pipeline | `app/pipeline.py` — rule matching + `evaluate_and_dispatch` (trigger cascade depth 3) |
| Actions / fields | `app/actions.py`, `app/fields.py` — action registry; Field sinks |
| Polling | `app/scheduler.py` + `app/pollers.py` — APScheduler + poller registry |
| Webhooks | `app/webhook_verifiers.py` — provider signature / replay verification |
| Live events | `app/event_stream.py` — in-process SSE fan-out for `/events/stream` |
| Source recipes | `app/source_templates.py` — full-stack quick-add prefills |
| Widgets | `app/widgets.py`, `app/widget_transforms.py`, `app/dashboard_layout.py` |
| Appearance | `app/themes.py`, `app/labels.py`, `app/webpush_util.py` |

Config lives in the DB. Config nav: `/config/pipeline`, `/config/users`, `/config/dashboard`, `/config/style`.

Flow: `webhook|poll → ingest_event → evaluate_and_dispatch → actions → Fields`; dashboard reads Fields via `widgets.fetch_widget_data`.

## Gotchas
- **No migrations** — schema is models + `Base.metadata.create_all()` at startup. Wipe `para_scope.db` / `.test_db.sqlite` when the model changes. Warn the user before wiping.
- **SQLite foreign keys are OFF by default** — `database.py` enables them via pragma. Rely on it.
- **Enum class names must not collide with model class names** — `ScheduleType` is the interval/cron enum for `PollingSchedule` (not to be confused with `EventTypeRecord`). Same table name (`event_types`) is fine; only the Python class name matters for relationships.
- **Auth middleware checks `session_username` cookie** — signed timed token, not a raw username. Any change to User or session needs a middleware update in `webctx`.
- **First-run setup** — with zero users, AuthMiddleware sends browsers to `/setup` (public). After the first user exists, `/setup` redirects to `/login`. Optional CLI: `create_user.py` (needs the DB file from a prior app start).
- **Polling jobs** — registered at startup and on poll source create/edit/delete. Jobs run in-process; never scale beyond 1 worker.
- **Rate-limit dicts live on `app.main`** — tests clear them via `main_mod._LOGIN_RATE_LIMIT.clear()` etc. If you add rate limiting elsewhere, re-export from `app.main`.
- **CSRF** — form POSTs need `csrf_token` cookie + `_csrf_token` form field; JSON POSTs need `X-CSRF-Token`. Webhooks / static / `/sw.js` skip CSRF. Auth/CSRF truth is `app/webctx.py`.
- **Soft JSON FKs on Rule** — `action_ids` / `event_type_ids` are JSON lists; scrub on delete via existing webctx helpers. Deletes cascade forward only (source → event type → rule → action); rule owns its actions.
- **`Rule.source_id` is NOT NULL** — no global rules. Schema wipe required when upgrading from nullable `source_id`.
- **`PARA_SCOPE_SECRET_KEY`** — required at process start (`main` lifespan); empty key aborts boot (tests set a default).
- **Trigger cascade depth 3** — nested `trigger_source` / Triggers widget calls share `evaluate_and_dispatch`’s ContextVar ceiling.
- **Dashboard widget text templates** — titles, labels, units, link URLs render `{{ slug… }}` at display time via `fields_snapshot`; notes text stays literal.

## Adding a new model
1. Define the class in `app/models.py` with `Base` inheritance.
2. Restart (or wipe the DB file if the schema changed) — tables are created automatically. **Warn the user** that wiping drops all config and data.

## CSS (STYLE.md)
Vanilla CSS, 37signals/Fizzy system. No build step. Files: `app/static/css/`.
- Use `@layer` cascade layers in `main.css`: reset → base → layout → components → utilities.
- OKLCH color tokens (`--lch-gray-*`, `--color-link`, etc.) with automatic dark mode via `prefers-color-scheme`.
- Custom properties as component APIs (e.g., `--btn-background`). Variants = one-line overrides.
- Naming: BEM-inspired (`.card`, `.card__header`, `.card--featured`). Flat file structure, one concept per file.

## DESIGN.md
Full design doc at root. Key entities: Source, EventTypeRecord, PollingSchedule, Rule, ActionInstance, Event, Field, AuditLog, DashboardLayout. Non-goals include multi-region HA, complex workflow engine, and Docker-first deployment.

## Directory AGENTS.md index

| Path | Covers |
|------|--------|
| [app/AGENTS.md](app/AGENTS.md) | Domain core modules |
| [app/routers/AGENTS.md](app/routers/AGENTS.md) | HTTP routers |
| [app/tests/AGENTS.md](app/tests/AGENTS.md) | pytest suite |
| [app/templates/AGENTS.md](app/templates/AGENTS.md) | Jinja pages |
| [app/templates/components/AGENTS.md](app/templates/components/AGENTS.md) | Shared includes |
| [app/templates/config/AGENTS.md](app/templates/config/AGENTS.md) | Config UI shell |
| [app/templates/config/pipeline/AGENTS.md](app/templates/config/pipeline/AGENTS.md) | Pipeline HTMX partials |
| [app/templates/widgets/AGENTS.md](app/templates/widgets/AGENTS.md) | Widget body partials |
| [app/static/AGENTS.md](app/static/AGENTS.md) | Static mount |
| [app/static/css/AGENTS.md](app/static/css/AGENTS.md) | CSS layers |
| [app/static/js/AGENTS.md](app/static/js/AGENTS.md) | Browser scripts |
| [app/static/vendor/AGENTS.md](app/static/vendor/AGENTS.md) | Third-party assets |
| [docs/AGENTS.md](docs/AGENTS.md) | README / marketing assets |
| [data/AGENTS.md](data/AGENTS.md) | Runtime uploads |

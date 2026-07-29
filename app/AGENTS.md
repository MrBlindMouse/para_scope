# app/ — Domain core

## Role

Single-process domain layer: SQLite models, auth/secrets, webhook/poll ingress, rule matching, action dispatch, Field sinks, and dashboard widget data. Routers are thin HTTP adapters — put business logic here.

## File map

| File | Owns |
|------|------|
| `main.py` | App factory, middleware order, static mount, router includes; re-exports rate-limit dicts for tests |
| `database.py` | Engine/session; SQLite pragmas (`foreign_keys`, WAL, busy_timeout) |
| `models.py` | All SQLAlchemy models + `ScheduleType` |
| `security.py` | bcrypt, CSRF mint, timed session tokens, Fernet encrypt/decrypt |
| `webctx.py` | Auth/CSRF middleware, templates+filters, form parsers, rate/replay, audit, webhook BG hook |
| `ingest.py` / `event_store.py` | Persist events + prune (never deletes `pending`) |
| `pipeline.py` | Rule match + conditions + `evaluate_and_dispatch` |
| `actions.py` | Action registry: field_push, http_forward, notify, web_push, local_script |
| `fields.py` | Field helpers, path access, star-binding ContextVar |
| `widgets.py` / `widget_transforms.py` / `dashboard_layout.py` | Widget registry, series/maths/templates, GridStack layout |
| `pollers.py` / `scheduler.py` | Poller registry + `run_schedule`; APScheduler lifecycle |
| `webhook_verifiers.py` | Provider signature / replay verification |
| `themes.py` / `labels.py` / `webpush_util.py` | Appearance, UI labels, VAPID config |

## Flow

```
webhook|poll → ingest_event → evaluate_and_dispatch → actions → Fields
dashboard → widgets.fetch_widget_data ← Fields (+ system tables)
```

## Invariants

- Schema = models + `create_all` only. Wipe DB on model change; **warn the user**.
- `PRAGMA foreign_keys=ON` via `database.py`. Single worker for rate limits, replay cache, scheduler, token caches.
- Soft JSON FKs on `Rule.action_ids` / `event_type_ids` — scrub on delete via webctx helpers.
- Paused `EventTypeRecord`: still ingest; rules skip.
- Event prune keeps `pending` (BackgroundTasks rely on it).
- CSRF exempt: `/webhook`, `/static`, `/sw.js`. Local scripts need `PARA_SCOPE_ALLOW_LOCAL_ACTIONS`.
- Star path bindings only inside `path_star_bindings` during dispatch.
- Enum class name ≠ model name (`ScheduleType` vs `EventTypeRecord`).

## Prefer / avoid

- **Prefer:** `register_action`, `register_poller`, `KIND_*` / binding tables; `ingest_event` for all Event writes; shared path/template helpers in `fields` + `widget_transforms`.
- **Prefer:** extend `actions` / `fields` / `pollers` / `widgets` over growing `webctx` unless the change is HTTP/middleware/forms.
- **Avoid:** new frameworks, Alembic, multi-worker assumptions, MetricPoint write paths for charts (Field logbooks drive series today).
- Large modules (`webctx`, `widgets`, `pollers`): extend carefully; split only when actively touching that area.

## See also

Root [AGENTS.md](../AGENTS.md) · [routers/AGENTS.md](routers/AGENTS.md) · [tests/AGENTS.md](tests/AGENTS.md) · [DESIGN.md](../DESIGN.md)

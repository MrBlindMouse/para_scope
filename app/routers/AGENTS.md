# app/routers/ — HTTP surface

## Role

FastAPI `APIRouter` modules included from `app/main.py`. Handlers stay thin over DB + `app.webctx` (templates, CSRF state, audit, form parsers). Auth and CSRF are **middleware**, not per-route dependencies.

## File map

| File | Owns |
|------|------|
| `auth.py` | `/setup`, `/login`, `/logout` |
| `webhook.py` | Public `/webhook/{slug}`, `/health`, `/sw.js` |
| `dashboard.py` | `/`, widgets, layout/notes APIs, push subscribe, `/config/dashboard` |
| `pipeline.py` | Pipeline config CRUD + HTMX dialogs (large — extend carefully) |
| `system.py` | Style/users/secrets/audit, events, metrics, help, system status |

Include order in `main.py`: auth → dashboard → pipeline → system → webhook.

## Invariants

- Auth/CSRF truth is `app/webctx.py` (`PUBLIC_PATHS` + CSRF exempt set). New public route → update **both** Auth and CSRF exempt lists.
- Webhooks: no session CSRF; verify via `webhook_verifiers`; ingest then `BackgroundTasks` → `webctx._process_webhook_event` (202 after accept).
- Mutating UI: **POST** + CSRF (cookie + form `_csrf_token` or `X-CSRF-Token`). Prefer POST over HTTP DELETE for UI deletes/toggles.
- HTMX: re-render dialog partial or `_dialog_success_response` / `HX-Redirect`. Pipeline success that closes the dialog sets `HX-Trigger: pipeline-dialog-close`.
- Path conventions: create under `/config/pipeline/…`; mutate often under `/config/source|event-type|rule|action/…` — keep consistent when adding routes.
- Setup serializes first user with SQLite `BEGIN IMMEDIATE`.

## Prefer / avoid

- Prefer existing `webctx` helpers (`_audit_log`, flash URLs, schedule/form parsers) over new router-local helpers.
- Do not put `/webhook` behind session auth.
- Do not grow `pipeline.py` unboundedly without splitting (dialogs vs CRUD) when you are already editing it.
- Defense-in-depth `_get_user` on JSON APIs is fine where present; do not invent a second auth system.

## See also

[app/AGENTS.md](../AGENTS.md) · root [AGENTS.md](../../AGENTS.md) · [tests/AGENTS.md](../tests/AGENTS.md)

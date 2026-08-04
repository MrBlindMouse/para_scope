# app/tests/ — pytest suite

## Role

HTTP integration via `TestClient` plus unit tests for pollers, actions, fields, themes. Real SQLite — no Docker, no external services. Fixtures live mainly in `test_app.py`; `conftest.py` pins the integration DB URL before collection.

## File map

| File | Owns |
|------|------|
| `conftest.py` | Pins `PARA_SCOPE_DATABASE_URL` → `app/.test_db.sqlite` + secret key before any test module imports `app.database` |
| `__init__.py` | Default `PARA_SCOPE_SECRET_KEY` for test env |
| `test_app.py` | Large HTTP/integration suite (auth, CRUD, webhooks, widgets, …) |
| `test_polling.py` | Poller handlers, `run_schedule`, scheduler jobs (local temp engine; patches `SessionLocal` with restore) |
| `test_phase4.py` | Actions (push/http/notify/script), jitter, WAL |
| `test_remaining.py` | AES secrets, webhook `always`/type rules |
| `test_fields.py` | Pure unit: paths, templates, maths |
| `test_feature_batch.py` | Triggers, SSE, dry-run, templates, fields namespace, self-metrics, cascade |
| `test_themes.py` | Theme options + contrast floors |
| `test_http_auth.py` | Shared outbound auth header helpers |

## Invariants

- Run: `pytest app/tests/ -v` (venv active). Wipe `app/.test_db.sqlite` after schema changes.
- `conftest.py` owns the global engine URL so collection order cannot rebind it. Isolated unit tests use their own `create_engine` / temp files without overriding `PARA_SCOPE_DATABASE_URL`.
- Rate-limit / replay dicts: clear via **`app.main`** re-exports (`_LOGIN_RATE_LIMIT`, `_WEBHOOK_RATE_LIMIT`, `_WEBHOOK_REPLAY_CACHE`) — same objects as `webctx`.
- CSRF: use `authenticated_client` / `CsrfClient` for authenticated form/JSON POSTs; webhooks skip CSRF. Raw `TestClient` when asserting CSRF rejection.
- `raise_server_exceptions=False` on the main client — template bugs may show as 500 HTML, not traceback failures.
- Class-scoped DB reset in `test_app` → state can accumulate within a class; uniquify names.

## Prefer / avoid

- Prefer `app.database.get_db` in `dependency_overrides` — **not** `main.get_db` (main does not export it).
- If patching `SessionLocal`, also patch `pollers.SessionLocal` / `scheduler.SessionLocal` (they bind the name at import) and restore in `finally`. `test_app` class teardown `drop_all`s the shared DB — later modules that start `TestClient` must not rely on those tables via the unpatched scheduler binding.
- Keep new HTTP cases near the matching `Test*` class in `test_app.py`; keep unit tests in the small focused files.
- Do not add Docker, fixtures frameworks, or mocked DB layers unless asked.
- `test_app.py` is large — extend carefully; split only when actively touching that area.

## See also

[app/AGENTS.md](../AGENTS.md) · root [AGENTS.md](../../AGENTS.md) · [routers/AGENTS.md](../routers/AGENTS.md)

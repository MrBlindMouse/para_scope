# app/templates/ — Jinja UI

## Role

Jinja2 HTML for all UI, mounted via `Jinja2Templates(directory="app/templates")` in `webctx.py`. Full pages plus HTMX partials.

## File map

| Kind | Pattern |
|------|---------|
| Shell | `base.html` — layout, CSRF meta, htmx, header |
| Pages | `{name}.html` — `index`, `events`, `metrics`, `system`, `login`, `setup`, … |
| Config | `config/` — see [config/AGENTS.md](config/AGENTS.md) |
| Components | `components/` — see [components/AGENTS.md](components/AGENTS.md) |
| Widgets | `widgets/` — see [widgets/AGENTS.md](widgets/AGENTS.md) |

## Invariants

- Most pages: `{% extends "base.html" %}` → `title`, `body_class`, `header_actions`, `content`.
- Auth pages (`login`, `setup`): **standalone** HTML (no `base`), CSRF-exempt.
- Mutating forms include `components/csrf.html` (`_csrf_token`). `base` sets `<meta name="csrf-token">` for JSON/`X-CSRF-Token`.
- Prefer partials over growing monolithic pages (`config/dashboard.html`, `config/help.html` are already large).

## Prefer / avoid

- Prefer shared includes under `components/` or `config/pipeline/` over copy-paste.
- Do not hand-roll CSRF hidden inputs.
- New widget kind → `widgets/{kind}_content.html` + registry in `app/widgets.py`.

## See also

[components/](components/AGENTS.md) · [config/](config/AGENTS.md) · [widgets/](widgets/AGENTS.md) · [app/AGENTS.md](../AGENTS.md)

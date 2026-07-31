# app/templates/widgets/ — Widget body partials

## Role

Widget body markup included on the dashboard and re-rendered by `GET /widgets/{type}`. Card chrome lives on `index.html` — these files are body-only.

## File map

`{kind}_content.html` for each `KIND_DISPLAYS` key:

`series`, `chart`, `display`, `clock`, `links`, `notes`, `system`, `triggers`

## Invariants

- Filename must be `{kind}_content.html` when adding a kind; also register in `app/widgets.py` (`KIND_DISPLAYS` / bindings).
- Context expects `wdata`, `widget_id`, `widget_config`, `display` (+ appearance).
- Titles/labels/units/link URLs may contain `{{ slug… }}`; expansion happens in `widgets.py` / dashboard `root()`, not in these Jinja bodies.
- BEM: `.widget-{kind}`, modifiers `--{display}` / `--{style}`, elements `__*`.
- Client hooks: `data-*-widget`, `data-chart-*`, etc. matching `static/js/widget-*.js`.
- HTMX refresh: non-`links` / `notes` / `clock` / `triggers` bodies get `hx-get="/widgets/{type}?id=…"` on a timer.

## Prefer / avoid

- New kind: template here + registry in `widgets.py` (+ CSS/JS only if needed).
- Do not wrap bodies in another `.card` — dashboard owns chrome.
- Prefer data attributes over inline script in templates.

## See also

[templates/AGENTS.md](../AGENTS.md) · [app/AGENTS.md](../../AGENTS.md) · [static/js/AGENTS.md](../../static/js/AGENTS.md)

# app/templates/config/ — Config UI

## Role

Config pages sharing `nav.html` shell (`config-page`, sidebar, disclosures/dialogs scripts).

## File map

| File | Owns |
|------|------|
| `nav.html` | Layout shell + nav links; `hx-boost` scoped to `<nav>` → `#config-panel` |
| `pipeline.html` | Pipeline page + `#pipeline-dialog` |
| `users.html`, `dashboard.html`, `style.html`, `audit_log.html`, `help.html` | Topic pages |
| `pipeline/` | HTMX partials — see [pipeline/AGENTS.md](pipeline/AGENTS.md) |

Naming: snake_case filenames matching routes (`audit_log` ↔ `/config/audit-log`).

## Invariants

- Pages extend `nav.html`; set `page_title` + `config_content`. Router context sets `active` nav item.
- Nav `hx-boost` targets/selects `#config-panel` only — do not put interactive HTMX inside the boosted nav.
- Keep page-specific scripts at the bottom of `config_content` (or after the panel) so listeners are not rebound away.
- Pipeline dialog: fill via `hx-target="#pipeline-dialog"`; close via `HX-Trigger: pipeline-dialog-close`.

## Prefer / avoid

- Heavy pages (`dashboard.html`, `help.html`): extract partials when touching, rather than growing further.
- Pair with CSS: `config-nav.css`, `pipeline.css`, `help.css`, `dashboard.css`.
- System is linked from config nav but uses root `templates/system.html` — do not invent a duplicate under `config/`.

## See also

[pipeline/AGENTS.md](pipeline/AGENTS.md) · [templates/AGENTS.md](../AGENTS.md)

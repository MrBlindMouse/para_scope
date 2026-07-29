# app/templates/components/ — Shared includes

## Role

Cross-page Jinja includes (not route pages). Plain `{% include %}` — no `{% extends %}`.

## File map

| File | Owns |
|------|------|
| `csrf.html` | Hidden `_csrf_token` for forms |
| `alerts.html` | `error` / `success` flash markup |
| `event_row.html` | `<tr>` fragment for events list / HTMX prepends |

## Invariants

- Form CSRF markup comes from `csrf.html` only — do not hand-roll the field.
- `event_row.html` is the HTMX fragment for `#events-tbody` swaps/prepends.

## Prefer / avoid

- New shared fragment → here, not under `config/`.
- Keep these thin; page-specific markup stays in the page or pipeline partials.

## See also

[templates/AGENTS.md](../AGENTS.md)

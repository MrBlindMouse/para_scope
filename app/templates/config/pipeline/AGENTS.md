# app/templates/config/pipeline/ — Pipeline HTMX partials

## Role

Underscore-prefixed partials for the pipeline dialog and source-chain SSR. Served into `#pipeline-dialog` or chain `outerHTML` swaps — never full pages.

## File map

| Pattern | Files |
|---------|--------|
| Forms | `_source_form`, `_source_edit_form`, `_event_form`, `_rule_form`, `_action_form`, `_field_form` |
| Chain | `_source_chain`, `_fields_section` |
| Recipes / dry-run | `_source_templates`, `_rule_test_result` |
| Shared fieldsets | `_webhook_provider_fields`, `_poll_schedule_fields`, `_conditions_builder` |
| Read-only dialogs | `_recent_events`, `_recent_logbook`, `_latest_event` |

Route ↔ template: `/config/pipeline/.../partials/{kebab}` → `_{snake}.html`.

## Invariants

- Leading `_` = partial only.
- Every POST form includes `components/csrf.html`.
- Dialog forms: `hx-post` + `hx-target="#pipeline-dialog"` + `hx-swap="innerHTML"`.
- Chain mutations: `hx-target="#source-chain-{{ id }}"` `outerHTML`.
- Success that should close the dialog: server sets `HX-Trigger: pipeline-dialog-close`.

## Prefer / avoid

- Reuse `_webhook_provider_fields`, `_poll_schedule_fields`, `_conditions_builder` — do not copy fieldsets into create vs edit.
- Prefer deduplicating `_source_form` / `_source_edit_form` when touching either.
- Pair with `static/js/source-dialog.js` and `conditions-builder.js`.

## See also

[config/AGENTS.md](../AGENTS.md) · [app/routers/AGENTS.md](../../../routers/AGENTS.md)

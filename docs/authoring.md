# Authoring guides

Para-Scope extensions stay **built-in**: register handlers in the existing
registries. Do not add plugin discovery, entry points, or a second packaging
system.

## Add a poller

1. Implement `handler(schedule, params, secrets) -> dict` in [`app/pollers.py`](../app/pollers.py).
2. Call `register_poller("my_handler", handler, spec={...})` at module import time.
3. Spec shape:
   - `label`, `category` (key of `POLLER_CATEGORY_META`), `summary`, `uses_url`
   - `fields`: list from `_field(name, label, ...)` — `param_key`, `input_type`,
     `parse_as`, `store` (`params`/`url`/`timeout`/`retry`), `secret`, `advanced`
4. Add a display label in `POLLER_LABELS` in [`app/labels.py`](../app/labels.py).
5. Form parsing is automatic via `parse_poller_form` — field `name` values must
   match the create/edit schedule form inputs.

See existing registrations near the bottom of `app/pollers.py` (e.g. `tcp_connect`,
`disk_free_space`).

## Add an action

Checklist (same path used by `trigger_source`):

1. Handler `def _action_*(db, event, action) -> None` in [`app/actions.py`](../app/actions.py).
2. `register_action("my_type", handler)`.
3. `ACTION_TYPE_LABELS` + `action_label` branch in [`app/labels.py`](../app/labels.py).
4. Parse branch in `webctx._parse_action_config`.
5. Panel `#panel-my-type` + JS `panels` map entry in
   [`app/templates/config/pipeline/_action_form.html`](../app/templates/config/pipeline/_action_form.html).
6. Draft rehydrate in `_build_action_dialog_context` in
   [`app/routers/pipeline.py`](../app/routers/pipeline.py).
7. Document on the Help page if the action is user-facing.

Action templates and rule conditions may use `fields.<slug>.<path>`. Reserved `field`
is still the Update Field target’s current value. Field slugs cannot be any name in
`RESERVED_FIELD_SLUGS` in [`app/fields.py`](../app/fields.py)
(`field`, `fields`, `value`, `source`, `_poll`, `dt`, `system`, `ts`).
Dashboard templates use the bare slug namespace (`{{ slug.value }}`), not `fields.*`.
Widget **titles**, **labels**, **units**, and **link URLs** accept the same `{{ … }}` templates at display time (notes text stays literal because it is live-edited).

## Add a widget kind

1. Add to `KIND_DISPLAYS`, `KIND_TITLES`, and usually `DISPLAY_TITLES` /
   `DISPLAY_STYLES` in [`app/widgets.py`](../app/widgets.py).
2. Bindings: `_BINDING_NONE` or a Field-binding dict in `WIDGET_BINDINGS`.
3. Data function + entry in `fetch_widget_data`’s `fn` map.
4. Body partial `app/templates/widgets/{kind}_content.html`.
5. Config editor fragment in [`app/templates/config/dashboard.html`](../app/templates/config/dashboard.html)
   (`configFieldsHtml` + read/save + add/remove handlers).
6. If the widget has client JS, add `app/static/js/{feature}.js` and load it from
   `index.html`. Skip HTMX auto-refresh for interactive kinds (see the
   `widget.type not in (...)` list on the dashboard).

## Source templates

Full-stack recipes live in [`app/source_templates.py`](../app/source_templates.py).
Each entry declares a poll source, Fields, rules/actions, and dashboard widgets.
`apply_source_template(db, slug)` creates the stack; the pipeline “From template”
picker POSTs to `/config/pipeline/templates/{slug}/apply`.

Recipe shape: `source`, `fields` (with `key` + preferred slug), `rules` (actions
reference fields by `field_key`), `widgets` (use `{key}` placeholders for Field
slugs). Prefer extending that module over a second creation path.

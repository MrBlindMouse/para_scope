"""Event processing pipeline — rule matching and action dispatch."""
import logging
import re

from app.actions import run_registered_action
from app.fields import get_by_path, path_star_bindings
from app.models import Event, EventTypeRecord, Rule, ActionInstance

logger = logging.getLogger("para_scope.pipeline")


def evaluate_rules(db, event):
    """Find all enabled rules that match the given event.

    A rule matches if:
    - rule.source_id is None (global rule) OR matches event.source_id
    - rule.event_type_ids is empty OR event.event_type_id is in rule.event_type_ids
    - rule.enabled is True

    Paused event types still ingest, but no rules run for them (including catch-alls).
    Untyped events (event_type_id null) are unaffected.
    """
    if event.event_type_id is not None:
        et = db.query(EventTypeRecord).filter(
            EventTypeRecord.id == event.event_type_id,
        ).first()
        if et is not None and not et.enabled:
            return []

    rules = db.query(Rule).filter(
        Rule.enabled == True,
        (Rule.source_id.is_(None)) | (Rule.source_id == event.source_id)
    ).order_by(Rule.order_index).all()

    matching = []
    for rule in rules:
        if rule.event_type_ids and event.event_type_id not in rule.event_type_ids:
            continue
        matching.append(rule)

    return matching


_KNOWN_OPS = frozenset({"not", "gt", "lt", "contains", "regex"})


def _match_value(actual, matcher) -> bool:
    """Return True if ``actual`` satisfies ``matcher``. Fail-closed except ``not``."""
    if isinstance(matcher, dict):
        if not matcher or set(matcher) - _KNOWN_OPS:
            return False
        if "not" in matcher:
            if actual == matcher["not"]:
                return False
        if "gt" in matcher:
            if actual is None:
                return False
            try:
                if float(actual) <= float(matcher["gt"]):
                    return False
            except (ValueError, TypeError):
                return False
        if "lt" in matcher:
            if actual is None:
                return False
            try:
                if float(actual) >= float(matcher["lt"]):
                    return False
            except (ValueError, TypeError):
                return False
        if "contains" in matcher:
            if actual is None:
                return False
            try:
                if matcher["contains"] not in str(actual):
                    return False
            except (TypeError, ValueError):
                return False
        if "regex" in matcher:
            if actual is None:
                return False
            try:
                if not re.search(matcher["regex"], str(actual)):
                    return False
            except re.error:
                return False
        return True
    return actual == matcher


def _path_has_star(path: str) -> bool:
    return "*" in path.split(".")


def _split_first_star(path: str) -> tuple[str, str]:
    """Split at the first ``*`` segment → (prefix before, suffix after)."""
    parts = path.split(".")
    i = parts.index("*")
    return ".".join(parts[:i]), ".".join(parts[i + 1 :])


def _match_conditions_on(
    data, conditions: dict, *, star_path: str = ""
) -> tuple[bool, dict[str, int]]:
    """AND-match conditions; on success return ``*`` prefix → list index bindings.

    ``star_path`` is the path of starred segments above this subtree (e.g.
    ``data.*``), used to build nested binding keys like ``data.*.items``.
    """
    plain = {}
    starred = {}
    for key, matcher in conditions.items():
        if _path_has_star(key):
            starred[key] = matcher
        else:
            plain[key] = matcher

    for key, matcher in plain.items():
        if not _match_value(get_by_path(data, key), matcher):
            return False, {}

    if not starred:
        return True, {}

    groups: dict[str, dict] = {}
    for key, matcher in starred.items():
        prefix, _ = _split_first_star(key)
        group_key = f"{prefix}.*" if prefix else "*"
        groups.setdefault(group_key, {})[key] = matcher

    bindings: dict[str, int] = {}
    for group_conds in groups.values():
        prefix, _ = _split_first_star(next(iter(group_conds)))
        lst = get_by_path(data, prefix) if prefix else data
        if not isinstance(lst, list):
            return False, {}

        if star_path:
            list_key = f"{star_path}.{prefix}" if prefix else star_path
        else:
            list_key = prefix

        matched_any = False
        for i, item in enumerate(lst):
            item_conds = {}
            for key, matcher in group_conds.items():
                _, suffix = _split_first_star(key)
                item_conds[suffix] = matcher
            child_star = f"{list_key}.*" if list_key else "*"
            ok, nested = _match_conditions_on(
                item, item_conds, star_path=child_star
            )
            if ok:
                bindings[list_key] = i
                bindings.update(nested)
                matched_any = True
                break
        if not matched_any:
            return False, {}

    return True, bindings


def match_conditions(data, conditions) -> tuple[bool, dict[str, int]]:
    """Evaluate conditions on a data dict; return (matched, star_bindings)."""
    if not conditions:
        return True, {}
    return _match_conditions_on(data, conditions)


def evaluate_conditions(event, conditions):
    """Evaluate rule conditions against event data.

    Supports:
      - Simple: {"field": "value"} — exact match
      - not:    {"field": {"not": "value"}} — not equal
      - gt/lt:  {"field": {"gt": 10}} / {"field": {"lt": 100}}
      - contains: {"field": {"contains": "substring"}}
      - regex:    {"field": {"regex": "^prefix"}}
      - List any: {"data.*.status": "fail"} — any list element matches
      - Correlated: {"data.*.base": "USD", "data.*.quote": "ZAR"} —
        one element satisfies all starred fields that share that list prefix
    All conditions must match (AND logic). Fail-closed: missing fields,
    unknown matcher keys, empty matcher dicts, and type/regex errors do not
    match (except ``not``, which matches when the value differs — including
    when the field is absent). Path ``*`` on a non-list does not match.

    When starred conditions match, the bound list indexes are applied to
    ``*`` in action/template paths for that rule's dispatch (see
    ``path_star_bindings``).
    """
    ok, _ = match_conditions(event.normalized_data or {}, conditions)
    return ok


def evaluate_and_dispatch(db, event):
    """Entry point: find matching rules and dispatch their actions."""
    rules = evaluate_rules(db, event)
    for rule in rules:
        ok, bindings = match_conditions(
            event.normalized_data or {}, rule.conditions or {}
        )
        if not ok:
            continue
        with path_star_bindings(bindings):
            for action_id in rule.action_ids:
                action = db.query(ActionInstance).filter(
                    ActionInstance.id == action_id,
                    ActionInstance.enabled == True,
                ).first()
                if action and (
                    action.source_id is None or action.source_id == event.source_id
                ):
                    _run_action(db, event, action)
    if event.status != "failed":
        event.status = "processed"
        db.commit()


def _run_action(db, event, action):
    """Execute a single action with retry and result tracking."""
    retries = (action.config or {}).get("retry_count", 0)
    error_messages = []

    for attempt in range(1 + retries):
        try:
            run_registered_action(db, event, action)
            return
        except Exception as e:
            error_messages.append(f"Try {attempt + 1}: {e}")
            if attempt < retries:
                continue
            event.status = "failed"
            event.processing_error = "; ".join(error_messages)
            logger.exception(
                "Action failed event_id=%s action_id=%s action_type=%s",
                event.id, action.id, action.action_type,
            )
            db.commit()

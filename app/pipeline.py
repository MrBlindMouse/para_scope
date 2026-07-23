"""Event processing pipeline — rule matching and action dispatch."""
import logging
import re

from app.actions import run_registered_action
from app.fields import get_by_path
from app.models import Event, Rule, ActionInstance

logger = logging.getLogger("para_scope.pipeline")


def evaluate_rules(db, event):
    """Find all enabled rules that match the given event.

    A rule matches if:
    - rule.source_id is None (global rule) OR matches event.source_id
    - rule.event_type_ids is empty OR event.event_type_id is in rule.event_type_ids
    - rule.enabled is True
    """
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


def evaluate_conditions(event, conditions):
    """Evaluate rule conditions against event data.

    Supports:
      - Simple: {"field": "value"} — exact match
      - not:    {"field": {"not": "value"}} — not equal
      - gt/lt:  {"field": {"gt": 10}} / {"field": {"lt": 100}}
      - contains: {"field": {"contains": "substring"}}
      - regex:    {"field": {"regex": "^prefix"}}
    All conditions must match (AND logic). Fail-closed: missing fields,
    unknown matcher keys, empty matcher dicts, and type/regex errors do not
    match (except ``not``, which matches when the value differs — including
    when the field is absent).
    """
    if not conditions:
        return True
    known_ops = frozenset({"not", "gt", "lt", "contains", "regex"})
    nd = event.normalized_data or {}
    for key, matcher in conditions.items():
        actual = get_by_path(nd, key)
        if isinstance(matcher, dict):
            if not matcher or set(matcher) - known_ops:
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
        else:
            # Simple exact match
            if actual != matcher:
                return False
    return True


def evaluate_and_dispatch(db, event):
    """Entry point: find matching rules and dispatch their actions."""
    rules = evaluate_rules(db, event)
    for rule in rules:
        if evaluate_conditions(event, rule.conditions):
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
            error_messages.append(f"Attempt {attempt + 1}: {e}")
            if attempt < retries:
                continue
            event.status = "failed"
            event.processing_error = "; ".join(error_messages)
            logger.exception(
                "Action failed event_id=%s action_id=%s action_type=%s",
                event.id, action.id, action.action_type,
            )
            db.commit()

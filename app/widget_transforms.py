"""Numeric transforms and safe expression eval for dashboard widgets."""
from __future__ import annotations

import ast
import operator
import re

from app.fields import get_by_path

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_COMPARE_OPS = {
    "gt": operator.gt,
    "lt": operator.lt,
    "gte": operator.ge,
    "lte": operator.le,
    "eq": operator.eq,
    "neq": operator.ne,
}
_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_*]+)*$")
_TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_NUMERIC_OPS = frozenset({"gt", "lt", "gte", "lte"})


def apply_ops(value, ops: list | None):
    """Apply ordered transform ops to a numeric value. Returns None on failure."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    for step in ops or []:
        if not isinstance(step, dict):
            continue
        op = (step.get("op") or "").strip().lower()
        if op == "abs":
            v = abs(v)
            continue
        if op == "round":
            try:
                digits = int(step.get("digits", 0))
            except (TypeError, ValueError):
                digits = 0
            v = round(v, digits)
            continue
        try:
            by = float(step.get("by", 0))
        except (TypeError, ValueError):
            return None
        if op == "mul":
            v = v * by
        elif op == "div":
            if by == 0:
                return None
            v = v / by
        elif op == "add":
            v = v + by
        elif op == "sub":
            v = v - by
        else:
            continue
    return v


def extract_number(data, value_path: str | None, ops: list | None = None):
    """Resolve a number from data (literal path or whole value) then apply ops."""
    if value_path:
        raw = get_by_path(data, value_path)
    else:
        raw = data
    return apply_ops(raw, ops)


def series_from_points(points, *, value_path: str | None = None, transform: list | None = None):
    """Build [{ts, v}] from iterable of (timestamp, value_payload) pairs."""
    series = []
    for ts, payload in points:
        v = extract_number(payload, value_path, transform)
        if v is None:
            continue
        iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        series.append({"ts": iso, "v": v})
    return series


def _eval_ast(node, data: dict):
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, data)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError("bad constant")
    # Python 3.7 compat: Num (removed in 3.14 but fine on 3.11)
    if isinstance(node, ast.Num):  # type: ignore[attr-defined]
        return float(node.n)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_ast(node.operand, data))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_ast(node.left, data)
        right = _eval_ast(node.right, data)
        if type(node.op) is ast.Div and right == 0:
            raise ZeroDivisionError
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Name):
        raw = get_by_path(data, node.id)
        if raw is None:
            raise ValueError("missing name")
        return float(raw)
    if isinstance(node, ast.Attribute):
        # dotted path built from Attribute chain → "a.b.c"
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            raise ValueError("bad attr")
        parts.append(cur.id)
        path = ".".join(reversed(parts))
        raw = get_by_path(data, path)
        if raw is None:
            raise ValueError("missing path")
        return float(raw)
    raise ValueError("disallowed")


def eval_expr(expr: str, data: dict | None) -> float | None:
    """Evaluate a safe arithmetic expression against ``data``. Fail-closed."""
    text = (expr or "").strip()
    if not text:
        return None
    try:
        tree = ast.parse(text, mode="eval")
        return float(_eval_ast(tree, data or {}))
    except Exception:
        return None


def _as_number(raw):
    """Return float if raw is numeric (bool excluded); else None."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def resolve_operand(expr: str, data: dict | None):
    """Path → raw; else numeric expr; else string literal. Missing path → literal."""
    text = (expr or "").strip()
    if not text:
        return None
    data = data or {}
    if _PATH_RE.fullmatch(text):
        raw = get_by_path(data, text)
        if raw is not None:
            return raw
    num = eval_expr(text, data)
    if num is not None:
        return num
    return text


def eval_compare(left_expr: str, op: str, right_expr: str, data: dict | None) -> bool:
    """Compare two operands. eq/neq allow strings; order ops require numbers."""
    op_key = (op or "").strip().lower()
    fn = _COMPARE_OPS.get(op_key)
    if fn is None:
        return False
    left = resolve_operand(left_expr, data)
    right = resolve_operand(right_expr, data)
    if left is None or right is None:
        return False
    left_n = _as_number(left)
    right_n = _as_number(right)
    try:
        if op_key in _NUMERIC_OPS:
            if left_n is None or right_n is None:
                return False
            return bool(fn(left_n, right_n))
        # eq / neq
        if left_n is not None and right_n is not None:
            return bool(fn(left_n, right_n))
        return bool(fn(str(left), str(right)))
    except Exception:
        return False


def format_expr_number(v: float) -> str:
    """Compact string for template substitution."""
    if v != v:  # NaN
        return ""
    if abs(v - round(v)) < 1e-12:
        return str(int(round(v)))
    s = f"{v:.12g}"
    return s


def resolve_path_or_expr(body: str, data: dict | None):
    """Prefer a resolving dotted path (keeps non-numeric values); else maths; else None."""
    text = (body or "").strip()
    if not text:
        return None
    data = data or {}
    if _PATH_RE.fullmatch(text):
        raw = get_by_path(data, text)
        if raw is not None:
            return raw
    return eval_expr(text, data)


def render_data_template(template: str, data: dict | None) -> str:
    """Substitute ``{{ path }}`` or ``{{ expr }}`` from ``data``."""
    data = data if isinstance(data, dict) else {}

    def repl(m):
        raw = resolve_path_or_expr(m.group(1), data)
        if raw is None:
            return ""
        if isinstance(raw, float):
            return format_expr_number(raw)
        return str(raw)

    return _TEMPLATE_RE.sub(repl, template or "")


def resolve_tone_rules(rules, data: dict | None) -> str:
    """First matching tone rule → positive|negative|neutral; else neutral."""
    allowed = ("positive", "negative", "neutral")
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        expr = rule.get("expr")
        compare = rule.get("compare")
        op = rule.get("op")
        tone = (rule.get("tone") or "").strip().lower()
        if tone not in allowed:
            continue
        if eval_compare(str(expr or ""), str(op or ""), str(compare or ""), data):
            return tone
    return "neutral"

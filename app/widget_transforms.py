"""Numeric transforms and safe expression eval for dashboard widgets."""
from __future__ import annotations

import ast
import json
import operator
import re
from datetime import datetime, timezone, timedelta

from app.fields import get_by_path

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_CALL_FUNCS = frozenset({"abs", "round", "min", "max"})
_COMPARE_OPS = {
    "gt": operator.gt,
    "lt": operator.lt,
    "gte": operator.ge,
    "lte": operator.le,
    "eq": operator.eq,
    "neq": operator.ne,
}
_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_*]+)*$")
# Dotted paths first so ``value.*.rate`` wins over bare ``value``.
_PATH_TOKEN_RE = re.compile(
    r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_*]+)+|[a-zA-Z_][a-zA-Z0-9_]*"
)
_TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_NUMERIC_OPS = frozenset({"gt", "lt", "gte", "lte"})


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


def _num_lit(v: float) -> str:
    """Embed a float as an AST-safe numeric literal."""
    if v != v:  # NaN
        raise ValueError("nan")
    if abs(v - round(v)) < 1e-12:
        s = str(int(round(v)))
    else:
        s = f"{v:.12g}"
    return f"({s})" if v < 0 else s


def _subst_path_tokens(text: str, data: dict) -> str | None:
    """Replace path tokens (incl. ``*`` segments) with numeric literals.

    Uses ``get_by_path`` so rule star bindings apply. Fail-closed: any path
    token that is not a number → None. Call names (``abs``, ``round``, …)
    followed by ``(`` are left alone.
    """
    matches = list(_PATH_TOKEN_RE.finditer(text))
    out = text
    for m in reversed(matches):
        token = m.group(0)
        if token in _CALL_FUNCS:
            after = text[m.end() :].lstrip()
            if after.startswith("("):
                continue
        raw = get_by_path(data, token)
        num = _as_number(raw)
        if num is None:
            return None
        try:
            lit = _num_lit(num)
        except ValueError:
            return None
        out = out[: m.start()] + lit + out[m.end() :]
    return out


def extract_number(data, value_path: str | None):
    """Resolve a number from data (literal path or whole value).

    Path ``value`` on a bare scalar (non-dict/list) uses the payload itself —
    same synthetic wrapper convention as Key/text logbook templates.
    """
    if value_path:
        raw = get_by_path(data, value_path)
        if raw is None and value_path == "value" and not isinstance(data, (dict, list)):
            raw = data
    else:
        raw = data
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def series_from_points(points, *, value_path: str | None = None):
    """Build [{ts, v}] from iterable of (timestamp, value_payload) pairs."""
    series = []
    for ts, payload in points:
        v = extract_number(payload, value_path)
        if v is None:
            continue
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        series.append({"ts": iso, "v": v})
    return series


def _ts_from_array_item(item, index: int):
    """Prefer item.ts / item.timestamp; else synthetic UTC from index."""
    if isinstance(item, dict):
        for key in ("ts", "timestamp", "t"):
            raw = item.get(key)
            if raw is None:
                continue
            if hasattr(raw, "isoformat"):
                return raw
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                # seconds vs ms heuristic
                sec = float(raw)
                if sec > 1e12:
                    sec = sec / 1000.0
                return datetime.fromtimestamp(sec, tz=timezone.utc)
            if isinstance(raw, str) and raw.strip():
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    pass
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)


def series_from_json_array(
    state,
    value_path: str,
    *,
    range_mode: str = "entries",
    range_entries: int = 50,
    cutoff=None,
):
    """Expand a Data-field JSON array path into [{ts, v}] points.

    - ``samples.*.ms`` maps over ``samples`` extracting ``ms``
    - ``temps`` (array of numbers) uses each element as the value
    - ``samples`` (array of objects) extracts ``value`` / ``v`` when present
    """
    path = (value_path or "").strip()
    if not path:
        return [], "Append a path"

    parts = path.split(".")
    item_path = None
    arr = None
    if "*" in parts:
        i = parts.index("*")
        list_path = ".".join(parts[:i])
        item_path = ".".join(parts[i + 1:]) or None
        arr = get_by_path(state, list_path) if list_path else state
    else:
        resolved = get_by_path(state, path)
        if isinstance(resolved, list):
            arr = resolved
        else:
            v = extract_number(state, path)
            if v is None:
                return [], "No numeric data"
            now = datetime.now(timezone.utc)
            return [{"ts": now.isoformat(), "v": v}], None

    if not isinstance(arr, list):
        return [], "Path must point to an array"

    pairs = []
    for idx, item in enumerate(arr):
        ts = _ts_from_array_item(item, idx)
        if item_path:
            pairs.append((ts, item))
        elif isinstance(item, dict):
            pairs.append((ts, item))
        else:
            pairs.append((ts, {"value": item}))

    extract_path = item_path
    if extract_path is None:
        # Prefer explicit value/v keys on objects; bare numbers wrapped as value
        extract_path = "value"
        if pairs and isinstance(pairs[0][1], dict):
            sample = pairs[0][1]
            if "value" not in sample and "v" in sample:
                extract_path = "v"

    series = series_from_points(pairs, value_path=extract_path)

    if range_mode == "entries":
        if range_entries > 0 and len(series) > range_entries:
            series = series[-range_entries:]
        return series, None

    if cutoff is not None:
        filtered = []
        for pt in series:
            try:
                ts = datetime.fromisoformat(pt["ts"].replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            # Keep points with real timestamps inside the window; drop pure synthetic epoch-index
            # points that fall before cutoff (index-based 1970… usually filtered out).
            if ts >= cutoff:
                filtered.append(pt)
        return filtered, None

    return series, None


def _eval_ast(node, data: dict):
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, data)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError("bad constant")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_ast(node.operand, data))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_ast(node.left, data)
        right = _eval_ast(node.right, data)
        if type(node.op) in (ast.Div, ast.Mod) and right == 0:
            raise ZeroDivisionError
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _CALL_FUNCS:
            raise ValueError("disallowed call")
        if node.keywords:
            raise ValueError("disallowed kwargs")
        args = [_eval_ast(a, data) for a in node.args]
        name = node.func.id
        if name == "abs":
            if len(args) != 1:
                raise ValueError("abs arity")
            return abs(args[0])
        if name == "round":
            if len(args) == 1:
                return float(round(args[0]))
            if len(args) == 2:
                return float(round(args[0], int(args[1])))
            raise ValueError("round arity")
        if name in ("min", "max"):
            if not args:
                raise ValueError("minmax arity")
            return float((min if name == "min" else max)(args))
        raise ValueError("disallowed call")
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
    """Evaluate a safe arithmetic expression against ``data``. Fail-closed.

    Path tokens (including starred segments like ``value.*.rate``) are resolved
    via ``get_by_path`` before AST eval so rule star bindings apply.
    """
    text = (expr or "").strip()
    if not text:
        return None
    data = data or {}
    try:
        rewritten = _subst_path_tokens(text, data)
        if rewritten is None:
            return None
        tree = ast.parse(rewritten, mode="eval")
        return float(_eval_ast(tree, data))
    except Exception:
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


def _resolve_shape(obj, data: dict):
    """Walk a JSON shape: string leaves → path, maths, or literal; else keep typed."""
    if isinstance(obj, dict):
        return {k: _resolve_shape(v, data) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_shape(v, data) for v in obj]
    if isinstance(obj, str):
        if _PATH_RE.fullmatch(obj):
            return get_by_path(data, obj)
        num = eval_expr(obj, data)
        return num if num is not None else obj
    return obj


def resolve_value_from_event(spec: str, data: dict | None):
    """Path, maths, or JSON shape → typed value.

    - Dotted path → value from ``data`` (objects/lists kept; missing → None).
    - Safe maths → float (``+ - * / %``, ``abs``, ``round``, ``min``, ``max``).
    - JSON object/array → same structure; string leaves are path, maths, or literal.
    """
    text = (spec or "").strip()
    if not text:
        return None
    data = data or {}
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return _resolve_shape(parsed, data)
    return resolve_path_or_expr(text, data)


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

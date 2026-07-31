"""Unit tests for dotted-path event data helpers."""

import pytest


def test_with_current_field_overwrites_event_key():
    from app.fields import with_current_field

    out = with_current_field({"field": "from-event", "status": "ok"}, 10)
    assert out["field"] == 10
    assert out["status"] == "ok"
    assert with_current_field(None, "x") == {"field": "x"}


def test_get_by_path_nested():
    from app.fields import get_by_path

    data = {"data": {"items": [{"id": 1}, {"id": 2}]}}
    assert get_by_path(data, "data.items.0.id") == 1
    assert get_by_path(data, "data.items") == [{"id": 1}, {"id": 2}]
    assert get_by_path(data, "") == data
    assert get_by_path(data, "missing.path") is None
    assert get_by_path({"_poll": {"response_time_ms": 273.5}}, "_poll.response_time_ms") == 273.5


def test_get_by_path_star_first_item():
    from app.fields import get_by_path

    data = {"data": [{"id": 1, "price": 10}, {"id": 2, "price": 20}]}
    assert get_by_path(data, "data.*.id") == 1
    assert get_by_path(data, "data.*.price") == get_by_path(data, "data.0.price")
    assert get_by_path({"data": []}, "data.*.id") is None
    assert get_by_path({"data": {"*": "dict-key"}}, "data.*") == "dict-key"
    assert get_by_path({"data": {"x": 1}}, "data.*.y") is None


def test_get_by_path_star_bindings():
    from app.fields import get_by_path, path_star_bindings

    data = {"value": [{"rate": 1}, {"rate": 2}, {"rate": 99}]}
    assert get_by_path(data, "value.*.rate", star_bindings={"value": 2}) == 99
    with path_star_bindings({"value": 1}):
        assert get_by_path(data, "value.*.rate") == 2
    assert get_by_path(data, "value.*.rate") == 1


def test_render_data_template_via_config_value():
    from app.widget_transforms import render_data_template

    nd = {"status": "ok", "rate": 20, "_poll": {"response_time_ms": 273.5}}
    assert render_data_template("x={{ status }}", nd) == "x=ok"
    assert render_data_template("{{ 1/rate }}", nd) == "0.05"
    assert render_data_template("{{ _poll.response_time_ms }}", nd) == "273.5"
    assert render_data_template("{{ missing }}", nd) == ""
    assert render_data_template("", nd) == ""


def test_render_data_template_star_path():
    from app.widget_transforms import render_data_template

    nd = {"data": [{"price": 9.5}, {"price": 1.0}]}
    assert render_data_template("{{ data.*.price }}", nd) == "9.5"
    assert render_data_template("{{ data.0.price }}", nd) == "9.5"


def test_resolve_numeric_dotted_path():
    from app.fields import resolve_numeric

    nd = {"_poll": {"response_time_ms": 273.5}}
    assert resolve_numeric("_poll.response_time_ms", nd) == 273.5
    assert resolve_numeric("2", nd) == 2.0


def test_resolve_numeric_maths():
    from app.fields import resolve_numeric

    nd = {"rate": 20, "qty": 3}
    assert resolve_numeric("rate * 2", nd) == 40.0
    assert resolve_numeric("1/rate", nd) == 0.05
    assert resolve_numeric("qty", nd) == 3.0


def test_resolve_bool_fixed():
    from app.fields import resolve_bool

    assert resolve_bool({"value": True}) is True
    assert resolve_bool({"value": False}) is False
    assert resolve_bool({"value": "on"}) is True
    assert resolve_bool({"value": "off"}) is False


def test_render_data_template():
    from app.widget_transforms import render_data_template

    nd = {"status": "ok", "rate": 20, "_poll": {"response_time_ms": 12}}
    assert render_data_template("x={{ status }}", nd) == "x=ok"
    assert render_data_template("{{ 1/rate }}", nd) == "0.05"
    assert render_data_template("ok {{ status }} {{ rate * 2 }}", nd) == "ok ok 40"
    assert render_data_template("{{ missing }}", nd) == ""
    assert render_data_template("t={{_poll.response_time_ms}}", nd) == "t=12"
    assert render_data_template("x={{missing.path}}", {"a": 1}) == "x="

    data = {"data": [{"price": 3}, {"price": 9}]}
    assert render_data_template("p={{data.*.price}}", data) == "p=3"
    assert render_data_template("p={{data.0.price}}", data) == "p=3"


def test_resolve_path_or_expr():
    from app.widget_transforms import resolve_path_or_expr

    nd = {"status": "ok", "rate": 20, "payload": {"items": [1, 2]}}
    assert resolve_path_or_expr("status", nd) == "ok"
    assert resolve_path_or_expr("payload.items", nd) == [1, 2]
    assert resolve_path_or_expr("rate * 2", nd) == 40.0
    assert resolve_path_or_expr("1/rate", nd) == 0.05
    assert resolve_path_or_expr("missing", nd) is None
    assert resolve_path_or_expr("1/0", nd) is None


def test_eval_expr_richer_maths():
    from app.widget_transforms import eval_expr

    nd = {"rate": 20, "x": -3.7, "a": 2, "b": 9}
    assert eval_expr("abs(x)", nd) == 3.7
    assert eval_expr("round(x)", nd) == -4.0
    assert eval_expr("round(rate / 3, 1)", nd) == 6.7
    assert eval_expr("min(a, b, rate)", nd) == 2.0
    assert eval_expr("max(a, b)", nd) == 9.0
    assert eval_expr("rate % 6", nd) == 2.0
    assert eval_expr("abs()", nd) is None
    assert eval_expr("__import__('os')", nd) is None


def test_eval_expr_trunc_sum_avg():
    from app.widget_transforms import eval_expr

    nd = {"a": 2.9, "b": 4, "c": 6}
    assert eval_expr("trunc(a)", nd) == 2.0
    assert eval_expr("trunc(-3.7)", nd) == -3.0
    assert eval_expr("sum(a, b, c)", nd) == 12.9
    assert eval_expr("avg(b, c)", nd) == 5.0
    assert eval_expr("sum()", nd) is None
    assert eval_expr("avg()", nd) is None
    assert eval_expr("trunc(a, b)", nd) is None
    # List paths are not accepted as aggregate args (variadic scalars only).
    assert eval_expr("sum(items)", {"items": [1, 2, 3]}) is None


def test_eval_expr_star_path_tokens():
    """Starred paths in maths resolve via get_by_path (rule star bindings)."""
    from app.fields import path_star_bindings
    from app.widget_transforms import eval_expr, resolve_value_from_event

    data = {
        "value": [
            {"quote": "EUR", "rate": 0.9},
            {"quote": "USD", "rate": 1.1},
        ]
    }
    # No bindings → * is index 0
    assert eval_expr("1 / value.*.rate", data) == pytest.approx(1 / 0.9)
    assert resolve_value_from_event("1 / value.*.rate", data) == pytest.approx(1 / 0.9)

    with path_star_bindings({"value": 1}):
        assert eval_expr("1 / value.*.rate", data) == pytest.approx(1 / 1.1)
        assert resolve_value_from_event("1 / value.*.rate", data) == pytest.approx(1 / 1.1)
        assert resolve_value_from_event("value.*.rate", data) == 1.1

    assert eval_expr("1 / value.*.missing", data) is None


def test_render_data_template_star_maths():
    """``{{ 1 / value.*.rate }}`` uses the same star bindings as paths."""
    from app.fields import path_star_bindings
    from app.widget_transforms import render_data_template

    data = {
        "value": [
            {"quote": "EUR", "rate": 0.9},
            {"quote": "USD", "rate": 1.1},
        ]
    }
    assert render_data_template("{{ 1 / value.*.rate }}", data) == "1.11111111111"
    with path_star_bindings({"value": 1}):
        assert render_data_template("r={{ value.*.rate }}", data) == "r=1.1"
        assert render_data_template("inv={{ 1 / value.*.rate }}", data) == "inv=0.909090909091"
        assert render_data_template(
            "{{ value.*.rate }} / {{ 1/value.*.rate }}", data
        ) == "1.1 / 0.909090909091"


def test_resolve_value_from_event_path_and_maths():
    from app.widget_transforms import resolve_value_from_event

    nd = {"status": "ok", "rate": 20, "payload": {"items": [1, 2]}}
    assert resolve_value_from_event("status", nd) == "ok"
    assert resolve_value_from_event("payload.items", nd) == [1, 2]
    assert resolve_value_from_event("rate * 2", nd) == 40.0
    assert resolve_value_from_event("abs(rate - 25)", nd) == 5.0
    assert resolve_value_from_event("missing", nd) is None


def test_resolve_value_from_event_shape():
    from app.widget_transforms import resolve_value_from_event

    nd = {
        "temp": 20,
        "payload": {"sensor": {"id": 1}},
        "field": {"n": 3},
    }
    shape = """{
      "label": "Sensor A",
      "celsius": "temp",
      "fahrenheit": "temp * 1.8 + 32",
      "raw": "payload.sensor",
      "next": "field.n + 1",
      "missing": "nope.path",
      "flag": true,
      "n": 1
    }"""
    out = resolve_value_from_event(shape, nd)
    assert out == {
        "label": "Sensor A",
        "celsius": 20,
        "fahrenheit": 68.0,
        "raw": {"id": 1},
        "next": 4.0,
        "missing": None,
        "flag": True,
        "n": 1,
    }
    assert resolve_value_from_event('["temp", "rate * 2", "hi there"]', {"temp": 5, "rate": 3}) == [
        5, 6.0, "hi there"
    ]
    # Invalid JSON that looks like shape → fall through to path/expr (fails → None)
    assert resolve_value_from_event("{not json", nd) is None

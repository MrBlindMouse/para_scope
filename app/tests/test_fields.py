"""Unit tests for dotted-path event data helpers."""


def test_get_by_path_nested():
    from app.fields import get_by_path

    data = {"data": {"items": [{"id": 1}, {"id": 2}]}}
    assert get_by_path(data, "data.items.0.id") == 1
    assert get_by_path(data, "data.items") == [{"id": 1}, {"id": 2}]
    assert get_by_path(data, "") == data
    assert get_by_path(data, "missing.path") is None
    assert get_by_path({"_poll": {"response_time_ms": 273.5}}, "_poll.response_time_ms") == 273.5


def test_resolve_string_dotted_path():
    from app.fields import resolve_string

    nd = {"_poll": {"response_time_ms": 273.5}, "status": "ok"}
    assert resolve_string({"value_key": "_poll.response_time_ms"}, nd) == "273.5"
    assert resolve_string({"value_key": "status"}, nd) == "ok"
    assert resolve_string({"value_key": "_poll.missing"}, nd) == ""


def test_resolve_numeric_dotted_path():
    from app.fields import resolve_numeric

    nd = {"_poll": {"response_time_ms": 273.5}}
    assert resolve_numeric("_poll.response_time_ms", nd) == 273.5
    assert resolve_numeric("2", nd) == 2.0


def test_resolve_bool_dotted_path():
    from app.fields import resolve_bool

    nd = {"flags": {"up": True}}
    assert resolve_bool({"value_key": "flags.up"}, nd) is True


def test_render_template_uses_get_by_path():
    from app.webpush_util import render_template

    assert render_template("t={{_poll.response_time_ms}}", {"_poll": {"response_time_ms": 12}}) == "t=12"
    assert render_template("x={{missing.path}}", {"a": 1}) == "x="

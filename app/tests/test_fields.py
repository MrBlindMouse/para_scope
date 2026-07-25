"""Unit tests for dotted-path event data helpers."""


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


def test_resolve_string_dotted_path():
    from app.fields import resolve_string

    nd = {"_poll": {"response_time_ms": 273.5}, "status": "ok"}
    assert resolve_string({"value_key": "_poll.response_time_ms"}, nd) == "273.5"
    assert resolve_string({"value_key": "status"}, nd) == "ok"
    assert resolve_string({"value_key": "_poll.missing"}, nd) == ""


def test_resolve_string_star_path():
    from app.fields import resolve_string

    nd = {"data": [{"price": 9.5}, {"price": 1.0}]}
    assert resolve_string({"value_key": "data.*.price"}, nd) == "9.5"
    assert resolve_string({"value_key": "data.0.price"}, nd) == "9.5"


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


def test_render_template_star_path():
    from app.webpush_util import render_template

    data = {"data": [{"price": 3}, {"price": 9}]}
    assert render_template("p={{data.*.price}}", data) == "p=3"
    assert render_template("p={{data.0.price}}", data) == "p=3"

"""Unit tests for shared outbound HTTP auth helpers."""
import pytest

from app.http_auth import inject_http_auth_headers


def test_inject_bearer_defaults():
    headers = inject_http_auth_headers({}, auth_mode="bearer", token="tok")
    assert headers == {"Authorization": "Bearer tok"}


def test_inject_bearer_custom_header_and_prefix():
    headers = inject_http_auth_headers(
        {"X-Extra": "1"},
        auth_mode="bearer",
        token="tok",
        config={"auth_header": "X-Token", "auth_prefix": ""},
    )
    assert headers == {"X-Extra": "1", "X-Token": "tok"}


def test_inject_key_secret_defaults():
    headers = inject_http_auth_headers(
        {},
        auth_mode="key_secret",
        api_key="PK",
        api_secret="SK",
    )
    assert headers == {"X-Api-Key": "PK", "X-Api-Secret": "SK"}


def test_inject_key_secret_custom_headers():
    headers = inject_http_auth_headers(
        {},
        auth_mode="key_secret",
        api_key="PK",
        api_secret="SK",
        config={
            "api_key_header": "APCA-API-KEY-ID",
            "api_secret_header": "APCA-API-SECRET-KEY",
        },
    )
    assert headers["APCA-API-KEY-ID"] == "PK"
    assert headers["APCA-API-SECRET-KEY"] == "SK"


def test_inject_key_secret_requires_both():
    with pytest.raises(ValueError, match="both credentials"):
        inject_http_auth_headers({}, auth_mode="key_secret", api_key="PK")
    with pytest.raises(ValueError, match="both credentials"):
        inject_http_auth_headers({}, auth_mode="key_secret", api_secret="SK")


def test_inject_none_and_unknown_are_noop():
    base = {"X-Static": "yes"}
    assert inject_http_auth_headers(dict(base), auth_mode="none", token="x") == base
    assert inject_http_auth_headers(dict(base), auth_mode="basic", token="x") == base
    assert inject_http_auth_headers(dict(base), auth_mode="", token="x") == base


def test_inject_bearer_without_token_leaves_headers():
    headers = {"X-Static": "yes"}
    assert inject_http_auth_headers(headers, auth_mode="bearer") == {"X-Static": "yes"}

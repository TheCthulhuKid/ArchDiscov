"""Shared JSON redaction and source-path sensitivity policy."""

from copy import deepcopy

import pytest
from pydantic import JsonValue

from archdiscov.models.sanitization import is_sensitive_path, sanitize_value


@pytest.mark.parametrize(
    "key",
    ["password", "AWS.SECRET_ACCESS-KEY"],
    ids=["exact", "normalized"],
)
def test_sensitive_keys(key: str) -> None:
    assert sanitize_value({key: "sensitive"}) == ({key: "[REDACTED]"}, [f"/{key}"])


@pytest.mark.parametrize(
    "key",
    ["FromPort", "SecretArn"],
    ids=["network-field", "secret-reference"],
)
def test_network_fields_and_references_are_not_sensitive(key: str) -> None:
    assert sanitize_value({key: "ordinary"}) == ({key: "ordinary"}, [])
    assert not is_sensitive_path(f"Properties.{key}")
    assert not is_sensitive_path(f"/Properties/{key}")


def test_recursive_redaction_and_nonmutation() -> None:
    value: JsonValue = {
        "items": [
            {"password": "hidden", "IpProtocol": "tcp", "FromPort": 443},
            [None, True, 1, 1.5, "normal", {"credentials": {"nested": "hidden"}}],
        ],
        "UserData": ["hidden", {"password": "also hidden"}],
    }
    original = deepcopy(value)
    sanitized, paths = sanitize_value(value)
    assert sanitized == {
        "items": [
            {"password": "[REDACTED]", "IpProtocol": "tcp", "FromPort": 443},
            [None, True, 1, 1.5, "normal", {"credentials": "[REDACTED]"}],
        ],
        "UserData": "[REDACTED]",
    }
    assert paths == ["/items/0/password", "/items/1/5/credentials", "/UserData"]
    assert value == original
    assert sanitized is not value
    assert isinstance(sanitized, dict)
    assert isinstance(value, dict)
    assert sanitized["items"] is not value["items"]
    assert sanitize_value(sanitized) == (sanitized, paths)


def test_json_pointer_escaping() -> None:
    value: JsonValue = {"a/b~c": {"": [{"db/password": "hidden", "~token": "hidden"}]}}
    assert sanitize_value(value) == (
        {"a/b~c": {"": [{"db/password": "[REDACTED]", "~token": "[REDACTED]"}]}},
        ["/a~1b~0c//0/db~1password", "/a~1b~0c//0/~0token"],
    )


@pytest.mark.parametrize("value", ["plain", [], {}], ids=["scalar", "list", "dict"])
def test_nonsensitive_root(value: JsonValue) -> None:
    assert sanitize_value(value) == (value, [])


def test_explicit_sensitive_root() -> None:
    value: JsonValue = {"password": "secret"}
    original = deepcopy(value)
    assert sanitize_value(value, sensitive=True) == ("[REDACTED]", [""])
    assert value == original
    assert sanitize_value("[REDACTED]", sensitive=True) == ("[REDACTED]", [""])


@pytest.mark.parametrize(
    "path",
    [
        "Resources.Database.Properties.MasterUserPassword",
        "items[0]['api-key']",
        'Properties["aws.secret-access_key"]',
        "/Properties/db~1password",
    ],
    ids=["dotted", "single-quoted", "double-quoted", "escaped-pointer"],
)
def test_sensitive_source_paths(path: str) -> None:
    assert is_sensitive_path(path)


@pytest.mark.parametrize(
    "path",
    ["", 'Properties["SecretArn"]', "Properties.passwordPolicy"],
    ids=["empty", "secret-reference", "sensitive-prefix"],
)
def test_nonsensitive_source_paths(path: str) -> None:
    assert not is_sensitive_path(path)

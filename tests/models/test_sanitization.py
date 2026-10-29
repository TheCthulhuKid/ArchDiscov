"""Shared JSON redaction and source-path sensitivity policy."""

from copy import deepcopy

import pytest
from pydantic import JsonValue

from archdiscov.models.sanitization import is_sensitive_path, sanitize_value


def test_normalized_sensitive_keys_are_redacted() -> None:
    key = "AWS.SECRET_ACCESS-KEY"
    assert sanitize_value({key: "sensitive"}) == ({key: "[REDACTED]"}, [f"/{key}"])


def test_secret_references_and_sensitive_prefixes_are_not_sensitive() -> None:
    assert sanitize_value({"SecretArn": "ordinary"}) == ({"SecretArn": "ordinary"}, [])
    assert not is_sensitive_path("Properties.SecretArn")
    assert not is_sensitive_path("/Properties/SecretArn")
    assert not is_sensitive_path("Properties.passwordPolicy")


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
    assert sanitize_value(sanitized) == (sanitized, paths)


def test_explicit_sensitive_root() -> None:
    value: JsonValue = {"password": "secret"}
    original = deepcopy(value)
    assert sanitize_value(value, sensitive=True) == ("[REDACTED]", [""])
    assert value == original
    assert sanitize_value("[REDACTED]", sensitive=True) == ("[REDACTED]", [""])


@pytest.mark.parametrize(
    "path",
    ["Resources.Database.Properties.MasterUserPassword", "items[0]['api-key']"],
    ids=["dotted", "quoted"],
)
def test_sensitive_source_paths(path: str) -> None:
    assert is_sensitive_path(path)

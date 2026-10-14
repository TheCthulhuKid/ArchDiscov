"""Shared redaction policy for JSON values and source configuration paths."""

import re

from pydantic import JsonValue

from .json_pointer import escape_pointer_token

__all__ = ["is_sensitive_path", "sanitize_value"]

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "masteruserpassword",
        "secret",
        "secretaccesskey",
        "awssecretaccesskey",
        "accesskeyid",
        "awsaccesskeyid",
        "sessiontoken",
        "awssessiontoken",
        "token",
        "apikey",
        "privatekey",
        "credentials",
        "authorization",
        "userdata",
        "connectionstring",
    }
)
_SENSITIVE_SUFFIXES = ("password", "token", "secret", "privatekey", "apikey")
_SOURCE_COMPONENTS = re.compile(r"\[\"([^\"]*)\"\]|\['([^']*)'\]|\[([^\]]*)\]|([^.[\]]+)")


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def is_sensitive_path(path: str) -> bool:
    """Recognize sensitive components in dot/bracket paths or RFC 6901 pointers."""
    if path.startswith("/"):
        components = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
    else:
        components = [
            next(part for part in match.groups() if part is not None)
            for match in _SOURCE_COMPONENTS.finditer(path)
        ]
    return any(_is_sensitive_key(component) for component in components)


def sanitize_value(value: JsonValue, *, sensitive: bool = False) -> tuple[JsonValue, list[str]]:
    """Copy JSON, redact sensitive values, and return their RFC 6901 paths.

    Sensitive containers are replaced as a whole. Paths follow input traversal
    order, and an explicitly sensitive root is recorded as the empty string.
    """
    paths: list[str] = []

    def visit(item: JsonValue, path: str, redact: bool = False) -> JsonValue:
        if redact:
            paths.append(path)
            return "[REDACTED]"
        if isinstance(item, dict):
            return {
                key: visit(
                    child,
                    f"{path}/{escape_pointer_token(key)}",
                    _is_sensitive_key(key),
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child, f"{path}/{index}") for index, child in enumerate(item)]
        return item

    return visit(value, "", sensitive), paths

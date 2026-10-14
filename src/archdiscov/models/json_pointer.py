"""Shared RFC 6901 helpers."""

from typing import Any


def escape_pointer_token(token: object) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def read_pointer(value: Any, pointer: str) -> Any:
    """Read a trusted, already-validated pointer, not an untrusted snapshot fact path."""
    for token in pointer.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value

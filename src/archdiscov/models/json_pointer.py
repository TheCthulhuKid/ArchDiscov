"""Shared RFC 6901 pointer token encoding."""


def escape_pointer_token(token: object) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")

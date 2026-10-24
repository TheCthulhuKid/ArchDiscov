"""A credential-shaped secret and every way a test checks that a value never shows it."""

import json
import traceback

from archdiscov.assistant.provider import ProviderError

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"


def secret_renderings(value: object) -> list[str]:
    """Every rendering of `value` a secret could leak through: repr, str, JSON, exceptions."""
    rendered = [repr(value), str(value)]
    if hasattr(value, "model_dump_json"):
        rendered += [value.model_dump_json(), json.dumps(value.model_dump(mode="json"))]
    if isinstance(value, BaseException):
        rendered.append("".join(traceback.format_exception_only(value)))
        rendered += [repr(getattr(value, name, None)) for name in ("details", "args")]
        if isinstance(value, ProviderError):
            rendered.append(json.dumps(value.diagnostic()))
    return rendered

"""Typed IAM resources and shared policy document normalization and redaction."""

import json
import math
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import Field, JsonValue, field_validator

from ..enums import ResourceType
from ..json_pointer import escape_pointer_token
from ..resource_base import Model, NonEmptyString, Resource
from ..sanitization import is_sensitive_path, sanitize_value

_DOCUMENT_ERROR = "IAM document must be a JSON object with Statement object(s)"
_SENSITIVE_CONTEXT = frozenset(
    {"externalid", "sourceidentity", "userid", "username", "email", "sub", "namequalifier"}
)


def _copy_json(value: Any) -> JsonValue:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(_DOCUMENT_ERROR)
        return {key: _copy_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_json(child) for child in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(_DOCUMENT_ERROR)


def normalize_document(value: Any) -> dict[str, JsonValue]:
    """Copy a policy object or decode JSON/percent-encoded JSON without exposing errors."""
    try:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = json.loads(unquote(value, errors="strict"))
        document = _copy_json(value)
        if not isinstance(document, dict):
            raise ValueError(_DOCUMENT_ERROR)
        statements = document.get("Statement")
        if not isinstance(statements, dict) and not (
            isinstance(statements, list) and all(isinstance(item, dict) for item in statements)
        ):
            raise ValueError(_DOCUMENT_ERROR)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError(_DOCUMENT_ERROR) from None
    return document


def _sensitive_context(key: str) -> bool:
    component = key.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    normalized = "".join(character for character in component.lower() if character.isalnum())
    return normalized in _SENSITIVE_CONTEXT or is_sensitive_path(component)


def sanitize_document(value: Any) -> tuple[dict[str, JsonValue], list[str]]:
    """Normalize and redact secrets, tag-pair values, and sensitive condition context.

    Returned paths are RFC 6901 pointers relative to the decoded document.
    Principal ARNs, actions, and non-sensitive condition values remain intact.
    """
    document, paths = sanitize_value(normalize_document(value))

    def visit(item: JsonValue, path: str, condition: bool = False) -> JsonValue:
        if isinstance(item, dict):
            result = {}
            tag_key = item.get("Key")
            sensitive_tag = isinstance(tag_key, str) and is_sensitive_path(tag_key)
            for key, child in item.items():
                pointer = f"{path}/{escape_pointer_token(key)}"
                if (key == "Value" and sensitive_tag) or (condition and _sensitive_context(key)):
                    result[key] = "[REDACTED]"
                    paths.append(pointer)
                else:
                    result[key] = visit(child, pointer, condition or key == "Condition")
            return result
        if isinstance(item, list):
            return [visit(child, f"{path}/{index}", condition) for index, child in enumerate(item)]
        return item

    sanitized = visit(document, "")
    return sanitized, list(dict.fromkeys(paths))


class IamInlinePolicy(Model):
    name: NonEmptyString
    document: dict[str, JsonValue]

    @field_validator("document", mode="before")
    @classmethod
    def sanitize_policy(cls, value: Any) -> dict[str, JsonValue]:
        return sanitize_document(value)[0]


class IamRole(Resource):
    resource_type: Literal[ResourceType.IAM_ROLE] = ResourceType.IAM_ROLE
    arn: NonEmptyString
    trust_document: dict[str, JsonValue] | None = None
    inline_policies: list[IamInlinePolicy] = Field(default_factory=list)
    boundary_arn: NonEmptyString | None = None

    @field_validator("trust_document", mode="before")
    @classmethod
    def sanitize_trust(cls, value: Any) -> dict[str, JsonValue] | None:
        return None if value is None else sanitize_document(value)[0]


class IamInstanceProfile(Resource):
    resource_type: Literal[ResourceType.INSTANCE_PROFILE] = ResourceType.INSTANCE_PROFILE
    arn: NonEmptyString
    role_arns: list[NonEmptyString] = Field(default_factory=list)


class IamManagedPolicy(Resource):
    resource_type: Literal[ResourceType.MANAGED_POLICY] = ResourceType.MANAGED_POLICY
    arn: NonEmptyString
    default_version_id: NonEmptyString | None = None
    document: dict[str, JsonValue] | None = None

    @field_validator("document", mode="before")
    @classmethod
    def sanitize_policy(cls, value: Any) -> dict[str, JsonValue] | None:
        return None if value is None else sanitize_document(value)[0]

"""SQS configuration models; no messages or effective permission conclusions."""

import re
from typing import Any, Literal, Self

from pydantic import (
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from ..enums import AwsService, ResourceType
from ..resource_base import NonEmptyString, Resource
from .iam import sanitize_document


def safe_policy(value: Any) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    try:
        document = sanitize_document(value)[0]
    except (ValueError, TypeError, RecursionError):
        return None

    def visit(item, condition=False):
        if isinstance(item, dict):
            return {
                key: (
                    "[REDACTED]"
                    if (key == "Value" and isinstance(item.get("Key"), str))
                    or (condition and "tag/" in key.lower())
                    else visit(child, condition or key == "Condition")
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child, condition) for child in item]
        return item

    return visit(document)


def safe_redrive_allow(value: Any) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict):
        return None
    permission = value.get("redrivePermission")
    if not isinstance(permission, str) or permission not in {"allowAll", "denyAll", "byQueue"}:
        return None
    result = {"redrivePermission": permission}
    if "sourceQueueArns" in value:
        arns = value["sourceQueueArns"]
        if not isinstance(arns, list) or not all(
            isinstance(arn, str)
            and re.fullmatch(
                r"arn:aws[a-z-]*:sqs:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]+(?:\.fifo)?", arn
            )
            for arn in arns
        ):
            return None
        result["sourceQueueArns"] = list(arns)
    if permission == "byQueue" and not 1 <= len(result.get("sourceQueueArns", [])) <= 10:
        return None
    return result


class SqsQueue(Resource):
    resource_type: Literal[ResourceType.SQS_QUEUE] = ResourceType.SQS_QUEUE
    arn: NonEmptyString
    queue_url: NonEmptyString
    policy: dict[str, JsonValue] | None = None
    retention_seconds: int | None = None
    visibility_timeout_seconds: int | None = None
    delay_seconds: int | None = None
    fifo_queue: bool | None = None
    content_based_deduplication: bool | None = None
    kms_master_key_id: NonEmptyString | None = None
    kms_data_key_reuse_period_seconds: int | None = None
    sqs_managed_sse_enabled: bool | None = None
    dead_letter_target_arn: NonEmptyString | None = None
    max_receive_count: int | None = None
    redrive_allow_policy: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_arn_identity(self) -> Self:
        if self.identity.service != AwsService.SQS or self.identity.physical_id != self.arn:
            raise ValueError("SQS identity must use the resource ARN and sqs service")
        return self

    @field_validator("policy", mode="before")
    @classmethod
    def sanitize_policy(cls, value: Any) -> dict[str, JsonValue] | None:
        return safe_policy(value)

    @field_validator("redrive_allow_policy", mode="before")
    @classmethod
    def sanitize_redrive_allow(cls, value: Any) -> dict[str, JsonValue] | None:
        return None if value is None else safe_redrive_allow(value)

    @model_serializer(mode="wrap")
    def sanitize_mutable_documents(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        safe = self.model_copy(
            update={
                "policy": safe_policy(self.policy),
                "redrive_allow_policy": safe_redrive_allow(self.redrive_allow_policy),
            }
        )
        data = handler(safe)
        if self.lifecycle is None:
            data.pop("lifecycle", None)
        return data

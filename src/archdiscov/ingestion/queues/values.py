"""Strict SQS configuration values, deliberately excluding workload counters."""

import json
import re
from typing import Any

from ...models.queues import safe_redrive_allow

INTEGER_FIELDS = {
    "MessageRetentionPeriod": ("retention_seconds", 60, 1209600),
    "VisibilityTimeout": ("visibility_timeout_seconds", 0, 43200),
    "DelaySeconds": ("delay_seconds", 0, 900),
    "KmsDataKeyReusePeriodSeconds": ("kms_data_key_reuse_period_seconds", 60, 86400),
    "MaximumMessageSize": (None, 1024, 1048576),
    "ReceiveMessageWaitTimeSeconds": (None, 0, 20),
    "CreatedTimestamp": (None, 0, None),
    "LastModifiedTimestamp": (None, 0, None),
}
BOOLEAN_FIELDS = {
    "FifoQueue": "fifo_queue",
    "ContentBasedDeduplication": "content_based_deduplication",
    "SqsManagedSseEnabled": "sqs_managed_sse_enabled",
}
ENUM_FIELDS = {
    "DeduplicationScope": {"messageGroup", "queue"},
    "FifoThroughputLimit": {"perQueue", "perMessageGroupId"},
}


def queue_arn(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"arn:aws[a-z-]*:sqs:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]+(?:\.fifo)?", value
        )
        is not None
    )


def queue_url(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"https://(?:sqs\.[a-z0-9-]+\.[a-z0-9.-]+|[a-z0-9-]+\.queue\.[a-z0-9.-]+)"
            r"/[0-9]{12}/[A-Za-z0-9_-]+(?:\.fifo)?",
            value,
        )
        is not None
    )


def integer(value: Any, minimum: int, maximum: int | None) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= minimum and (maximum is None or number <= maximum) else None


def decoded(value: Any) -> dict | None:
    try:
        item = json.loads(value) if isinstance(value, str) else value
        return item if isinstance(item, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def redrive(value: Any) -> dict | None:
    item = decoded(value)
    if item is None or not queue_arn(item.get("deadLetterTargetArn")):
        return None
    raw_count = item.get("maxReceiveCount")
    if type(raw_count) is int:
        raw_count = str(raw_count)
    count = integer(raw_count, 1, 1000)
    if count is None:
        return None
    return {"deadLetterTargetArn": item["deadLetterTargetArn"], "maxReceiveCount": count}


def redrive_allow(value: Any) -> dict | None:
    item = safe_redrive_allow(decoded(value))
    if item is None:
        return None
    arns = item.get("sourceQueueArns", [])
    if not all(queue_arn(arn) for arn in arns):
        return None
    if item["redrivePermission"] == "byQueue" and not 1 <= len(arns) <= 10:
        return None
    return item


def kms_key(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"(?:[a-fA-F0-9-]{36}|alias/[A-Za-z0-9/_-]+|"
            r"arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:(?:key|alias)/[A-Za-z0-9/_-]+)",
            value,
        )
        is not None
    )

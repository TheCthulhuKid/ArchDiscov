"""Stable validation codes and public messages, separate from private error details."""

from dataclasses import dataclass
from types import MappingProxyType

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

SAFE_VALIDATION_MESSAGES = MappingProxyType(
    {
        "snapshot_duplicate_resource": "Snapshot constraint failed: duplicate resource.",
        "snapshot_evidence_snapshot_mismatch": (
            "Snapshot constraint failed: snapshot_id must match snapshot."
        ),
        "snapshot_evidence_owner_unknown": "Snapshot constraint failed: unknown resource owner.",
        "snapshot_evidence_citation_unknown": (
            "Snapshot constraint failed: unknown evidence citation."
        ),
        "snapshot_evidence_citation_owner_mismatch": (
            "Snapshot constraint failed: belongs to another resource."
        ),
        "snapshot_unresolved_resource_unknown": (
            "Snapshot constraint failed: unknown unresolved resource."
        ),
        "snapshot_collection_window_invalid": (
            "Snapshot constraint failed: collection_completed_at must not precede "
            "collection_started_at."
        ),
        "snapshot_operation_window_invalid": (
            "Snapshot constraint failed: operation coverage timestamps must fall within "
            "snapshot collection."
        ),
        "snapshot_fact_pointer_missing_key": "Snapshot constraint failed: missing key.",
        "snapshot_fact_pointer_invalid_index": "Snapshot constraint failed: invalid array index.",
        "snapshot_fact_pointer_index_out_of_bounds": (
            "Snapshot constraint failed: array index out of bounds."
        ),
        "snapshot_fact_pointer_scalar": "Snapshot constraint failed: cannot traverse scalar.",
        "cloud_snapshot_source_platform_mismatch": (
            "Snapshot constraint failed: source platform must match snapshot scope."
        ),
        "cloud_snapshot_coverage_scope_mismatch": (
            "Snapshot constraint failed: coverage scope must match snapshot scope."
        ),
        "cloud_snapshot_locations_uncovered": (
            "Snapshot constraint failed: snapshot locations must be included in coverage."
        ),
        "cloud_snapshot_scope_coverage_required": (
            "Snapshot constraint failed: scope_resources require scope_wide coverage."
        ),
        "cloud_snapshot_resource_location_mismatch": (
            "Snapshot constraint failed: location must match enclosing location."
        ),
        "cloud_snapshot_evidence_context_mismatch": (
            "Snapshot constraint failed: evidence context must match owning resource."
        ),
        "cloud_snapshot_aws_evidence_scope_required": (
            "Snapshot constraint failed: AWS evidence requires AWS scope."
        ),
        "cloud_snapshot_azure_evidence_scope_required": (
            "Snapshot constraint failed: Azure evidence requires Azure scope."
        ),
        "cloud_snapshot_lifecycle_source_invalid": (
            "Snapshot constraint failed: lifecycle requires AWS Terraform plan source semantics."
        ),
        "cloud_snapshot_dependency_source_unknown": (
            "Snapshot constraint failed: unknown source_id."
        ),
        "cloud_snapshot_dependency_target_unknown": (
            "Snapshot constraint failed: unknown target_id."
        ),
        "cloud_snapshot_dependency_location_mismatch": (
            "Snapshot constraint failed: dependency grouping must match source resource location."
        ),
        "azure_resource_native_type_mismatch": (
            "Snapshot constraint failed: Azure identity native_type must match resource type."
        ),
        "tool_input_schema_not_object": (
            "Conversation constraint failed: tool input schema must describe an object."
        ),
        "tool_input_schema_too_large": (
            "Conversation constraint failed: tool input schema exceeds the size limit."
        ),
        "tool_arguments_too_large": (
            "Conversation constraint failed: tool arguments exceed the size limit."
        ),
        "assistant_message_empty": (
            "Conversation constraint failed: assistant message needs text or tool calls."
        ),
        "conversation_system_message_position": (
            "Conversation constraint failed: system messages must precede all other messages."
        ),
        "conversation_tool_call_id_duplicate": (
            "Conversation constraint failed: duplicate tool call ID."
        ),
        "conversation_tool_result_orphan": (
            "Conversation constraint failed: tool result references no earlier tool call."
        ),
        "conversation_tool_result_duplicate": (
            "Conversation constraint failed: tool call already has a result."
        ),
        "provider_parallel_calls_without_tools": (
            "Provider constraint failed: parallel tool calls require tool calling."
        ),
        "provider_tool_name_duplicate": "Provider constraint failed: duplicate tool name.",
        "provider_stop_reason_mismatch": (
            "Provider constraint failed: tool_use stop reason must match tool calls."
        ),
        "provider_config_secret_field": (
            "Provider configuration constraint failed: credential fields are not accepted; "
            "reference an environment variable or profile instead."
        ),
        "provider_config_secret_value": (
            "Provider configuration constraint failed: value looks like a secret."
        ),
        "provider_config_limit_order": (
            "Provider configuration constraint failed: per-request limits exceed run limits."
        ),
        "provider_config_endpoint_invalid": (
            "Provider configuration constraint failed: endpoint must use HTTPS (or loopback "
            "HTTP) without credentials, query, or fragment."
        ),
        "fake_transcript_error_unscriptable": (
            "Fake transcript constraint failed: capability errors are raised by checks, "
            "not scripted."
        ),
        "fake_transcript_capability_mismatch": (
            "Fake transcript constraint failed: a turn's tool calls need a capability the "
            "transcript does not declare."
        ),
        "tool_truncation_reasons_order": (
            "Tool output constraint failed: truncation reasons must be unique and ordered."
        ),
        "tool_truncation_counts": (
            "Tool output constraint failed: truncation counts must match the returned items."
        ),
        "tool_error_message_mismatch": (
            "Tool output constraint failed: error message must be the fixed message for its code."
        ),
        "tool_returned_ids_order": (
            "Tool output constraint failed: returned IDs must be sorted and unique."
        ),
        "provider_config_region_invalid": (
            "Provider configuration constraint failed: region must be a concrete AWS region."
        ),
    }
)


def validation_error(code: str, detail: str) -> PydanticCustomError:
    """Keep detailed direct validation errors; public renderers must use only the code."""
    if code not in SAFE_VALIDATION_MESSAGES:
        raise ValueError(f"Unregistered validation code: {code}")
    return PydanticCustomError(code, "{detail}", {"detail": detail})


@dataclass(frozen=True)
class ValidationIssue:
    location: tuple[str | int, ...]
    type: str
    message: str


def safe_validation_issues(
    error: ValidationError, fields: frozenset[str], subject: str
) -> tuple[ValidationIssue, ...]:
    """Issues from a Pydantic error without untrusted keys, discriminator tags, or inputs.

    Dynamic input keys and tags can occur in Pydantic locations, so only known field names and
    list positions are kept; messages, inputs, and contexts are never exposed.
    """
    return tuple(
        ValidationIssue(
            tuple(
                part if isinstance(part, int) or part in fields else "field" for part in item["loc"]
            ),
            item["type"],
            f"Invalid {subject} field.",
        )
        for item in error.errors(include_input=False, include_context=False)
    )


def serialization_issue(subject: str) -> ValidationIssue:
    return ValidationIssue((), "serialization_error", f"{subject} is not serializable.")

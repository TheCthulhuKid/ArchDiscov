"""Stable validation codes and public messages, separate from private error details."""

from types import MappingProxyType

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
    }
)


def validation_error(code: str, detail: str) -> PydanticCustomError:
    """Keep detailed direct validation errors; public renderers must use only the code."""
    if code not in SAFE_VALIDATION_MESSAGES:
        raise ValueError(f"Unregistered validation code: {code}")
    return PydanticCustomError(code, "{detail}", {"detail": detail})

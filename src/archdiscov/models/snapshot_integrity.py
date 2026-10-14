import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import JsonValue

from .resource_base import Resource
from .validation_errors import validation_error


class EvidenceRecord(Protocol):
    @property
    def snapshot_id(self) -> UUID: ...

    @property
    def resource_id(self) -> str: ...


class UnresolvedRecord(Protocol):
    @property
    def resource_id(self) -> str | None: ...


class OperationRecord(Protocol):
    @property
    def started_at(self) -> datetime: ...

    @property
    def completed_at(self) -> datetime: ...

    @property
    def unresolved_values(self) -> Sequence[UnresolvedRecord]: ...


def index_resources(
    roots: Iterable[Resource],
    location: str,
    resources: dict[str, Resource],
    locations: dict[str, str],
    validate_placement: Callable[[Resource, str], None],
) -> None:
    for position, resource in enumerate(roots):
        current = f"{location}[{position}]"
        if resource.id in resources:
            raise validation_error(
                "snapshot_duplicate_resource",
                f"duplicate resource {resource.id!r} at {locations[resource.id]} and {current}",
            )
        validate_placement(resource, current)
        resources[resource.id] = resource
        locations[resource.id] = current
        index_resources(
            resource.children, f"{current}.children", resources, locations, validate_placement
        )


def validate_evidence(
    snapshot_id: UUID,
    resources: Mapping[str, Resource],
    evidence: Mapping[str, EvidenceRecord],
) -> None:
    for evidence_id, item in evidence.items():
        if item.snapshot_id != snapshot_id:
            raise validation_error(
                "snapshot_evidence_snapshot_mismatch",
                f"evidence {evidence_id!r}: snapshot_id must match snapshot",
            )
        if item.resource_id not in resources:
            raise validation_error(
                "snapshot_evidence_owner_unknown",
                f"evidence {evidence_id!r}: unknown resource owner {item.resource_id!r}",
            )


def validate_resource_facts(
    resources: Mapping[str, Resource], evidence: Mapping[str, EvidenceRecord]
) -> None:
    representations: dict[str, dict[str, Any]] = {}
    for resource in resources.values():
        _validate_citations(resource.evidence_ids, resource.id, evidence)
        if resource.fact_evidence and resource.id not in representations:
            _cache_fact_representations(resource, resource.model_dump(mode="json"), representations)
        for pointer, evidence_ids in resource.fact_evidence.items():
            _validate_fact_pointer(representations[resource.id], pointer, resource.id)
            _validate_citations(evidence_ids, resource.id, evidence)


def validate_unresolved_resources(
    operations: Iterable[OperationRecord], resources: Mapping[str, Resource]
) -> None:
    for position, operation in enumerate(operations):
        for unresolved in operation.unresolved_values:
            if unresolved.resource_id is not None and unresolved.resource_id not in resources:
                raise validation_error(
                    "snapshot_unresolved_resource_unknown",
                    f"coverage.operations[{position}]: unknown unresolved resource "
                    f"{unresolved.resource_id!r}",
                )


def validate_collection_window(
    start: datetime, end: datetime, operations: Iterable[OperationRecord]
) -> None:
    if end < start:
        raise validation_error(
            "snapshot_collection_window_invalid",
            "collection_completed_at must not precede collection_started_at",
        )
    for item in operations:
        if not (start <= item.started_at <= item.completed_at <= end):
            raise validation_error(
                "snapshot_operation_window_invalid",
                "operation coverage timestamps must fall within snapshot collection",
            )


def _cache_fact_representations(
    resource: Resource,
    representation: dict[str, Any],
    representations: dict[str, dict[str, Any]],
) -> None:
    representations[resource.id] = representation
    for child, child_representation in zip(
        resource.children, representation["children"], strict=True
    ):
        _cache_fact_representations(child, child_representation, representations)


def _validate_citations(
    evidence_ids: list[str], resource_id: str, evidence: Mapping[str, EvidenceRecord]
) -> None:
    for evidence_id in evidence_ids:
        if evidence_id not in evidence:
            raise validation_error(
                "snapshot_evidence_citation_unknown",
                f"resource {resource_id!r}: unknown evidence citation {evidence_id!r}",
            )
        if evidence[evidence_id].resource_id != resource_id:
            raise validation_error(
                "snapshot_evidence_citation_owner_mismatch",
                f"resource {resource_id!r}: evidence citation {evidence_id!r} "
                "belongs to another resource",
            )


def _validate_fact_pointer(value: JsonValue, pointer: str, resource_id: str) -> None:
    for encoded in pointer.split("/")[1:]:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token in value:
                value = value[token]
                continue
            reason = f"missing key {token!r}"
            code = "snapshot_fact_pointer_missing_key"
        elif isinstance(value, list):
            if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
                reason = f"invalid array index {token!r}"
                code = "snapshot_fact_pointer_invalid_index"
            elif len(token) > len(str(len(value))) or int(token) >= len(value):
                reason = f"array index out of bounds {token!r}"
                code = "snapshot_fact_pointer_index_out_of_bounds"
            else:
                value = value[int(token)]
                continue
        else:
            reason = "cannot traverse scalar"
            code = "snapshot_fact_pointer_scalar"
        raise validation_error(
            code, f"resource {resource_id!r}: fact pointer {pointer!r}: {reason}"
        )

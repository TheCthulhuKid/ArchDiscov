"""Search and inspect recorded resources through an already-built graph index."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from archdiscov.graph.identity import PhysicalResourceKey, native_resource_type
from archdiscov.models.enums import CloudPlatform, QueryErrorCode, QueryTruncationReason
from archdiscov.models.query.requests import QueryInspectRequest, QuerySearchRequest
from archdiscov.models.query.results import QueryInspectResult, QuerySearchResult, QueryTruncation
from archdiscov.models.resource_base import Model, Resource

from .query_context import (
    coverage_limitations,
    missing_coverage,
    no_coverage_limitation,
    resource_coverage,
    tag_provenance,
)
from .query_errors import QueryError
from .validation_data import detach_data

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


def validate_request[Request: Model](model: type[Request], request: Any) -> Request:
    try:
        return model.model_validate(detach_data(request))
    except (ValidationError, PydanticSerializationError, ValueError) as error:
        raise QueryError(QueryErrorCode.INVALID_REQUEST) from error


def _matches(resource: Resource, request: QuerySearchRequest) -> bool:
    if request.resource_id is not None and resource.id != request.resource_id:
        return False
    if request.name is not None and resource.name != request.name:
        return False
    if request.native_type is not None:
        actual, expected = native_resource_type(resource), request.native_type
        if resource.identity.platform == CloudPlatform.AZURE:
            actual, expected = actual.lower(), expected.lower()
        if actual != expected:
            return False
    return True


def search_resources(
    index: GraphIndex, request: QuerySearchRequest | Mapping[str, Any]
) -> QuerySearchResult:
    """AND-combine exact filters, preserving all qualified physical candidates.

    Coverage limitations describe snapshot context, not proof of a particular
    omitted match. Tag provenance is checked before tag exclusion, so uncertainty
    survives an empty result. Only omitted matches cause result truncation.
    """
    request = validate_request(QuerySearchRequest, request)
    scope = index.scope
    physical = request.physical_identifier
    key = (
        PhysicalResourceKey.from_values(physical.scope, physical.native_type, physical.physical_id)
        if physical is not None
        else None
    )
    coverage = index.coverage_records()
    limitations = coverage_limitations(coverage)
    matches = []
    evidence = []
    # resources() copies the forest once, rather than each descendant subtree.
    for resource in index.resources():
        if not _matches(resource, request):
            continue
        if key is not None and index.physical_key(resource.id) != key:
            continue
        evidence.extend(index.evidence_for_resource(resource.id))
        if request.tag is not None:
            limitation = tag_provenance(index, resource, request.tag.key)
            if limitation is not None:
                limitations.append(limitation)
            tags = resource.attributes.get("tags")
            if not isinstance(tags, dict) or request.tag.key not in tags:
                continue
            value = tags[request.tag.key]
            if not isinstance(value, str):
                continue
            if request.tag.value is not None and (
                "[REDACTED]" in value or request.tag.value != value
            ):
                continue
        matches.append(resource)
    limitations.extend(missing_coverage(index, evidence))
    return QuerySearchResult(
        snapshot_id=index.snapshot_id,
        scope=scope,
        resources=matches[: request.limit_result],
        limitations=limitations,
        truncation=QueryTruncation(
            reasons=[QueryTruncationReason.RESULT_LIMIT]
            if len(matches) > request.limit_result
            else []
        ),
    )


def inspect_resource(
    index: GraphIndex, request: QueryInspectRequest | Mapping[str, Any]
) -> QueryInspectResult:
    """Return detached subtype data, all owner evidence, and contextual coverage.

    Include uncited/unavailable owner records as well as exact fact citations.
    Same-scope/location/service coverage is context, not a completeness claim.
    """
    request = validate_request(QueryInspectRequest, request)
    resource = index.get_resource(request.resource_id)
    if resource is None:
        raise QueryError(QueryErrorCode.RESOURCE_NOT_FOUND)
    scope = index.scope
    evidence = {
        evidence_id: index.get_evidence(evidence_id)
        for evidence_id in index.resource_evidence_ids(resource.id)
    }
    coverage = resource_coverage(index, resource)
    limitations = coverage_limitations(coverage)
    limitations.extend(missing_coverage(index, evidence.values()))
    if not coverage:
        limitations.append(no_coverage_limitation(resource))
    tags = tag_provenance(index, resource)
    if tags is not None:
        limitations.append(tags)
    return QueryInspectResult(
        snapshot_id=index.snapshot_id,
        scope=scope,
        resource=resource,
        evidence=evidence,
        coverage=coverage,
        limitations=limitations,
    )

"""Recorded query context, without inferring collection or tag completeness."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from archdiscov.models.cloud.contracts import CloudEvidence, CloudOperationCoverage
from archdiscov.models.coverage_key import CoverageKey
from archdiscov.models.enums import CoverageStatus, EvidenceValueStatus, SourceType
from archdiscov.models.json_pointer import escape_pointer_token
from archdiscov.models.query.limitations import (
    QueryCoverageReference,
    QueryLimitation,
    QueryLimitedCoverageLimitation,
    QueryMissingCoverageLimitation,
    QueryProvenanceLimitation,
)
from archdiscov.models.resource_base import Resource

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


def coverage_limitations(records: Iterable[CloudOperationCoverage]) -> list[QueryLimitation]:
    return [
        QueryLimitedCoverageLimitation(
            coverage=QueryCoverageReference(
                scope=record.scope,
                location=record.location,
                service=record.service,
                operation=record.operation,
            ),
            status=record.status,
            message="Recorded operation coverage is limited.",
        )
        for record in records
        if record.status != CoverageStatus.SUCCESS
    ]


def missing_coverage(index: GraphIndex, evidence: Iterable[CloudEvidence]) -> list[QueryLimitation]:
    """Only name missing operations explicitly identified by AWS API locators.

    File locators are not operation names. Do not guess expected API inventories
    for another cloud or for an IaC source.
    """
    references = {}
    for record in evidence:
        if record.source != SourceType.AWS:
            continue
        locator = record.locator.removeprefix(f"{record.service}:")
        if re.fullmatch(r"[A-Z][A-Za-z0-9]+", locator) is None:
            continue
        reference = QueryCoverageReference(
            scope=record.scope,
            location=record.location,
            service=record.service,
            operation=locator,
        )
        key = CoverageKey.from_record(reference)
        if index.get_coverage(key) is None:
            references.setdefault(key, reference)
    return [
        QueryMissingCoverageLimitation(
            coverage=reference,
            message="No coverage record is available for this cited operation.",
        )
        for reference in references.values()
    ]


def tag_provenance(
    index: GraphIndex, resource: Resource, key: str | None = None
) -> QueryProvenanceLimitation | None:
    """An explicit present citation is needed; whole-item evidence is insufficient.

    Per-key citations establish that key's provenance, not completeness of the
    entire tag set. A redacted/omitted/unresolved citation is not a known value.
    """
    tags = resource.attributes.get("tags")
    pointer = "/attributes/tags"
    pointers = [pointer]
    if key is not None and isinstance(tags, dict) and key in tags:
        pointer += f"/{escape_pointer_token(key)}"
        pointers.append(pointer)
    records = [record for path in pointers for record in index.evidence_for_fact(resource.id, path)]
    if (
        isinstance(tags, dict)
        and all(isinstance(value, str) for value in tags.values())
        and any(record.value_status == EvidenceValueStatus.PRESENT for record in records)
    ):
        return None
    return QueryProvenanceLimitation(
        resource_id=resource.id,
        fact_path=pointer,
        message="Tag provenance is unavailable or incomplete; missing tags do not prove absence.",
    )

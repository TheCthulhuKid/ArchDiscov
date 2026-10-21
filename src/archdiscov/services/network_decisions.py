"""Decision construction, composition, and qualification for network-path primitives.

Three-valued logic with conservative defaults: nothing is allowed unless every needed
element is observed and allows. Results describe recorded configuration, never live
reachability.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import CoverageReference
from archdiscov.models.enums import (
    CloudPlatform,
    CoverageStatus,
    NetworkBlockerCode,
    NetworkDecision,
)
from archdiscov.models.network_path import NetworkBlocker, NetworkResult, compose_decisions
from archdiscov.models.resource_base import Resource

from .query_context import contextual_coverage

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

# Whether an ordered entry matches: True, False, or None when that cannot be decided.
Match = bool | None


def blocker(
    code: NetworkBlockerCode,
    message: str,
    *,
    element: str | None = None,
    resource_id: str | None = None,
    evidence_ids: Iterable[str] = (),
    coverage: CoverageReference | None = None,
) -> NetworkBlocker:
    return NetworkBlocker(
        code=code,
        message=message,
        element=element,
        resource_id=resource_id,
        evidence_ids=sorted(set(evidence_ids)),
        coverage=coverage,
    )


def allow(evidence_ids: Iterable[str]) -> NetworkResult:
    return NetworkResult(decision=NetworkDecision.ALLOW, evidence_ids=sorted(set(evidence_ids)))


def deny(blockers: Iterable[NetworkBlocker]) -> NetworkResult:
    return _result(NetworkDecision.DENY, [], blockers)


def unknown(blockers: Iterable[NetworkBlocker]) -> NetworkResult:
    return _result(NetworkDecision.UNKNOWN, [], blockers)


def _single(
    code: NetworkBlockerCode, message: str, element: str | None, resource_id: str | None
) -> NetworkResult:
    return unknown([blocker(code, message, element=element, resource_id=resource_id)])


def missing(
    message: str, *, element: str | None = None, resource_id: str | None = None
) -> NetworkResult:
    """Unknown because required evidence was not recorded."""
    return _single(NetworkBlockerCode.MISSING_EVIDENCE, message, element, resource_id)


def unsupported(
    message: str, *, element: str | None = None, resource_id: str | None = None
) -> NetworkResult:
    """Unknown because the element is outside the supported catalogue."""
    return _single(NetworkBlockerCode.UNSUPPORTED_ELEMENT, message, element, resource_id)


def _key(item: NetworkBlocker) -> tuple:
    return (
        item.code,
        item.element or "",
        item.resource_id or "",
        item.message,
        tuple(item.evidence_ids),
    )


def _result(
    decision: NetworkDecision, evidence_ids: Iterable[str], blockers: Iterable[NetworkBlocker]
) -> NetworkResult:
    """Deduplicated, deterministically ordered blockers and evidence."""
    unique = {_key(item): item for item in blockers}
    return NetworkResult(
        decision=decision,
        evidence_ids=sorted(set(evidence_ids)),
        blockers=[unique[key] for key in sorted(unique)],
    )


def _with(items: list[NetworkResult], decision: NetworkDecision) -> list[NetworkResult]:
    return [result for result in items if result.decision == decision]


def _conclude(decision: NetworkDecision, decided: list[NetworkResult]) -> NetworkResult:
    """The composition's result from the results that decided it."""
    if decision == NetworkDecision.ALLOW:
        return allow(e for result in decided for e in result.evidence_ids)
    return _result(
        decision,
        (e for result in decided for e in result.evidence_ids),
        (item for result in decided for item in result.blockers),
    )


def all_of(results: Iterable[NetworkResult]) -> NetworkResult:
    """Every needed element must allow: deny dominates, then unknown, then allow.

    An empty composition is unknown, never a vacuous allow.
    """
    items = list(results)
    if not items:
        return missing("No elements were evaluated.")
    decision = compose_decisions(result.decision for result in items)
    return _conclude(decision, _with(items, decision))


def any_of(results: Iterable[NetworkResult]) -> NetworkResult:
    """Alternatives: allow dominates, then unknown, then deny (only when all deny).

    No alternatives is unknown, never a deny.
    """
    items = list(results)
    if not items:
        return missing("No alternatives were evaluated.")
    for decision in (NetworkDecision.ALLOW, NetworkDecision.UNKNOWN):
        if decided := _with(items, decision):
            return _conclude(decision, decided)
    return _conclude(NetworkDecision.DENY, items)


def first_match(
    entries: Iterable[tuple[Match, NetworkResult]], default: NetworkResult
) -> NetworkResult:
    """Ordered evaluation: the first matching entry decides, else `default`.

    An entry that cannot be matched or ruled out makes the whole result unknown, because
    skipping it could change the outcome.
    """
    for matched, result in entries:
        if matched is None:
            return result if result.decision == NetworkDecision.UNKNOWN else _undecidable(result)
        if matched:
            return result
    return default


def _undecidable(result: NetworkResult) -> NetworkResult:
    return unknown(
        [
            blocker(
                NetworkBlockerCode.MISSING_EVIDENCE,
                "An earlier ordered entry could not be matched or ruled out.",
                evidence_ids=result.evidence_ids,
            )
        ]
    )


def qualify_coverage(
    index: GraphIndex,
    result: NetworkResult,
    resources: Iterable[Resource],
    operations: Collection[str] | None = None,
) -> NetworkResult:
    """Turn an apparent allow or deny into unknown when relevant collection was limited.

    Relevant records are those recorded for the resources' scope, location, and service,
    optionally only the named operations. Successful-zero collection is not a limitation,
    and absent records are not interpreted (a primitive reports missing data itself).
    """
    records, _ = contextual_coverage(index, resources)
    limited = [
        record
        for record in records
        if record.status != CoverageStatus.SUCCESS
        and (operations is None or record.operation in operations)
    ]
    if not limited:
        return result
    blockers = [
        blocker(
            NetworkBlockerCode.COVERAGE_LIMITED,
            f"Recorded {record.service} operation {record.operation} is {record.status}.",
            element=record.operation,
            coverage=CoverageReference(
                scope=record.scope,
                location=record.location,
                service=record.service,
                operation=record.operation,
            ),
        )
        for record in limited
    ]
    if result.decision != NetworkDecision.UNKNOWN:
        return _result(NetworkDecision.UNKNOWN, result.evidence_ids, blockers)
    return _result(NetworkDecision.UNKNOWN, result.evidence_ids, [*result.blockers, *blockers])


def require_aws(index: GraphIndex) -> NetworkResult | None:
    """The shared unsupported result for non-AWS snapshots; None when AWS."""
    platform = index.scope.platform
    if platform == CloudPlatform.AWS:
        return None
    return unknown(
        [
            blocker(
                NetworkBlockerCode.UNSUPPORTED_PLATFORM,
                f"Network-path evaluation supports AWS only; {platform} networking is not "
                "evaluated. Analyze an AWS snapshot instead.",
            )
        ]
    )

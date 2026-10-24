"""Qualify a comparison by lifecycle, source kind, and recorded coverage.

A planned Terraform deletion or replacement is a planned lifecycle, never an observed absence.
A resource missing from a side whose recorded coverage for its context failed, was incomplete,
skipped, or unsupported is qualified rather than reported as removed, and a context with no
recorded coverage is reported as unrecorded without inventing an operation. Coverage is
selected with the shared contextual coverage helper, so comparisons read it exactly as analyzers
do.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import CoverageReference, ResourceReference
from archdiscov.models.canonical import canonical_json
from archdiscov.models.comparison.entries import (
    AmbiguousMatch,
    FieldDifference,
    MatchedPair,
    Qualification,
    UnmatchedResource,
)
from archdiscov.models.comparison.results import ComparisonLimitation, ComparisonSnapshot
from archdiscov.models.enums import (
    ComparisonLimitationKind,
    ComparisonSide,
    CoverageStatus,
    QualificationKind,
)
from archdiscov.models.resource_base import Resource

from .comparison_matching import AmbiguousIds, MatchedIds
from .query_context import contextual_coverage

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

LEFT, RIGHT = ComparisonSide.LEFT, ComparisonSide.RIGHT
_LIFECYCLE_POINTER = "/lifecycle/actions"


def comparison_snapshot(side: ComparisonSide, index: GraphIndex) -> ComparisonSnapshot:
    """The labelled side of a comparison, taken from the snapshot."""
    started, completed = index.collection_window
    return ComparisonSnapshot(
        side=side,
        snapshot_id=index.snapshot_id,
        platform=index.scope.platform,
        scope=index.scope,
        source_kind=index.source_kind,
        source_label=index.source_label,
        collection_started_at=started,
        collection_completed_at=completed,
    )


def snapshot_limitations(
    left: GraphIndex, right: GraphIndex, reasons: Iterable[ComparisonLimitationKind]
) -> list[ComparisonLimitation]:
    """Snapshot-level limitations: the given reasons plus a source kind mismatch."""
    messages = {
        ComparisonLimitationKind.CROSS_CLOUD: (
            "The snapshots record different clouds, so no resource can match."
        ),
        ComparisonLimitationKind.SCOPE_MISMATCH: (
            "The snapshots record different scopes, so no resource can match."
        ),
        ComparisonLimitationKind.RESULT_LIMIT_REACHED: (
            "Entries were limited by the result limit; more exist."
        ),
    }
    kinds = set(reasons)
    if left.source_kind != right.source_kind:
        kinds.add(ComparisonLimitationKind.SOURCE_KIND_MISMATCH)
        messages[ComparisonLimitationKind.SOURCE_KIND_MISMATCH] = (
            f"The left snapshot is {left.source_kind} and the right is {right.source_kind}: "
            "differences are between what each records, not verified drift."
        )
    return [
        ComparisonLimitation(kind=kind, message=messages[kind]) for kind in sorted(kinds, key=str)
    ]


class EntryBuilder:
    """Build qualified entries for two indexes (build once per comparison)."""

    def __init__(self, left: GraphIndex, right: GraphIndex) -> None:
        self._index = {LEFT: left, RIGHT: right}
        # Coverage of one side says nothing about a resource of a different scope.
        self._comparable = left.scope == right.scope

    def matched(self, pair: MatchedIds, differences: list[FieldDifference]) -> MatchedPair:
        """A matched pair with each side's planned lifecycle, and coverage on its differences."""
        ids = {LEFT: pair.left_id, RIGHT: pair.right_id}
        lifecycle = [
            item for side in (LEFT, RIGHT) if (item := self._lifecycle(side, ids[side])) is not None
        ]
        coverage = (
            [
                item
                for side in (LEFT, RIGHT)
                for item in self._coverage(side, self._resource(side, ids[side]))
            ]
            if differences
            else []
        )
        return MatchedPair(
            left=self._reference(LEFT, pair.left_id),
            right=self._reference(RIGHT, pair.right_id),
            basis=pair.basis,
            differences=[_with_qualifications(item, coverage) for item in differences],
            qualifications=_ordered(lifecycle),
        )

    def unmatched(self, side: ComparisonSide, resource_id: str) -> UnmatchedResource:
        """An unmatched resource: its planned lifecycle, and the other side's coverage.

        The resource is absent from the other side, so what that side recorded for the
        resource's context says whether the absence is meaningful.
        """
        resource = self._resource(side, resource_id)
        qualifications = [] if (item := self._lifecycle(side, resource_id)) is None else [item]
        if self._comparable:
            qualifications += self._coverage(side.opposite, resource)
        return UnmatchedResource(
            side=side,
            resource=self._reference(side, resource_id),
            qualifications=_ordered(qualifications),
        )

    def ambiguous(self, group: AmbiguousIds) -> AmbiguousMatch:
        """An ambiguous match listing every candidate on each side."""

        def references(side: ComparisonSide, ids: tuple[str, ...]) -> list[ResourceReference]:
            ordered = sorted(ids, key=canonical_json)  # the contract's candidate order
            return [self._reference(side, resource_id) for resource_id in ordered]

        return AmbiguousMatch(
            basis=group.basis,
            key=group.key,
            left=references(LEFT, group.left_ids),
            right=references(RIGHT, group.right_ids),
        )

    # ---- helpers -------------------------------------------------------------------

    def _resource(self, side: ComparisonSide, resource_id: str) -> Resource:
        resource = self._index[side].get_resource(resource_id)
        if resource is None:
            raise ValueError("entries are built from the same indexes")
        return resource

    def _reference(self, side: ComparisonSide, resource_id: str) -> ResourceReference:
        return ResourceReference(resource_id=resource_id, scope=self._index[side].scope)

    def _lifecycle(self, side: ComparisonSide, resource_id: str) -> Qualification | None:
        lifecycle = self._resource(side, resource_id).lifecycle
        if lifecycle is None:
            return None
        cited = self._index[side].fact_evidence_ids(resource_id, _LIFECYCLE_POINTER)
        return Qualification(
            kind=QualificationKind.PLANNED_LIFECYCLE,
            side=side,
            lifecycle=lifecycle,
            evidence_ids=sorted(set(cited)),
        )

    def _coverage(self, side: ComparisonSide, resource: Resource) -> list[Qualification]:
        """Limited operations recorded for the resource's context, or its unrecorded service."""
        records, uncovered = contextual_coverage(self._index[side], [resource])
        found = [
            Qualification(
                kind=QualificationKind.LIMITED_COVERAGE,
                side=side,
                coverage=CoverageReference(
                    scope=record.scope,
                    location=record.location,
                    service=record.service,
                    operation=record.operation,
                ),
            )
            for record in records
            if record.status != CoverageStatus.SUCCESS
        ]
        if uncovered:
            service = str(resource.identity.service)
            found.append(
                Qualification(
                    kind=QualificationKind.UNRECORDED_COVERAGE, side=side, service=service
                )
            )
        return found


def _ordered(items: Iterable[Qualification]) -> list[Qualification]:
    """Unique qualifications in the contract's canonical order."""
    unique = {canonical_json(item.model_dump(mode="json")): item for item in items}
    return [unique[key] for key in sorted(unique)]


def _with_qualifications(
    difference: FieldDifference, extra: list[Qualification]
) -> FieldDifference:
    return difference.model_copy(
        update={"qualifications": _ordered([*difference.qualifications, *extra])}
    )

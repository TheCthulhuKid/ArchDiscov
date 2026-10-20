from collections.abc import Sequence
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ..analysis.references import ResourceReference
from ..cloud.scope import SupportedScope
from ..enums import CloudPlatform, ComparisonLimitationKind, ComparisonSide, SnapshotSourceKind
from ..resource_base import Model, NonEmptyString
from .entries import AmbiguousMatch, MatchedPair, UnmatchedResource, require_canonical

DEFAULT_RESULT_LIMIT = 1000
MAX_RESULT_LIMIT = 10000


class ComparisonSnapshot(Model):
    """One labelled side of a comparison, so left and right are never swapped silently.

    The label is taken from the snapshot: its platform and complete scope, what kind of source
    it came from (an observation, a Terraform state or plan, or a CloudFormation declaration),
    and when it was collected.
    """

    side: ComparisonSide
    snapshot_id: UUID
    platform: CloudPlatform
    scope: SupportedScope
    source_kind: SnapshotSourceKind
    source_label: NonEmptyString | None = None
    collection_started_at: AwareDatetime
    collection_completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_side(self) -> Self:
        if self.scope.platform != self.platform:
            raise ValueError("scope platform must match the snapshot platform")
        if self.collection_completed_at < self.collection_started_at:
            raise ValueError("collection must not complete before it starts")
        return self


class ComparisonLimitation(Model):
    """A snapshot-level limitation of the comparison itself (the message is not identity)."""

    kind: ComparisonLimitationKind
    message: NonEmptyString


class ComparisonResult(Model):
    """The recorded differences and qualified absences between two selected snapshots.

    A comparison is a separate immutable result, not an analysis. It reports what the two
    snapshots record, never proof that anything was added, removed, or has drifted. Every
    resource appears in at most one entry on its side, and every list is in canonical order.
    Each kind of entry (and each side's unmatched resources) is bounded by `result_limit`, and a
    `result_limit_reached` limitation reports that more entries exist.
    """

    schema_version: Literal["1"] = "1"
    left: ComparisonSnapshot
    right: ComparisonSnapshot
    created_at: AwareDatetime
    result_limit: int = Field(default=DEFAULT_RESULT_LIMIT, ge=1, le=MAX_RESULT_LIMIT)
    matched: list[MatchedPair] = Field(default_factory=list)
    unmatched: list[UnmatchedResource] = Field(default_factory=list)
    ambiguous: list[AmbiguousMatch] = Field(default_factory=list)
    limitations: list[ComparisonLimitation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (self.left.side, self.right.side) != (ComparisonSide.LEFT, ComparisonSide.RIGHT):
            raise ValueError("left and right must be labelled as such")
        for entries, label in (
            (self.matched, "matched"),
            (self.unmatched, "unmatched"),
            (self.ambiguous, "ambiguous"),
        ):
            require_canonical(entries, lambda entry: entry.id, label)
        for entries in (self.matched, self.ambiguous):
            self._require_within_limit(entries)
        self._require_within_limit(
            [item for item in self.unmatched if item.side == ComparisonSide.LEFT]
        )
        self._require_within_limit(
            [item for item in self.unmatched if item.side == ComparisonSide.RIGHT]
        )
        require_canonical(self.limitations, lambda item: item.kind, "limitations")
        self._validate_references()
        return self

    def _require_within_limit(self, entries: Sequence[object]) -> None:
        if len(entries) > self.result_limit:
            raise ValueError("a list of entries must not exceed the result limit")

    def _validate_references(self) -> None:
        """Scopes match their side, and no resource is claimed by two entries."""
        sides = {ComparisonSide.LEFT: self.left.scope, ComparisonSide.RIGHT: self.right.scope}
        seen: set[tuple[ComparisonSide, str]] = set()
        for side, reference in self._references():
            if reference.scope != sides[side]:
                raise ValueError("a resource reference must carry the scope of its side")
            if (side, reference.resource_id) in seen:
                raise ValueError("a resource may appear in only one comparison entry")
            seen.add((side, reference.resource_id))

    def _references(self) -> list[tuple[ComparisonSide, ResourceReference]]:
        found = [(ComparisonSide.LEFT, pair.left) for pair in self.matched]
        found += [(ComparisonSide.RIGHT, pair.right) for pair in self.matched]
        found += [(item.side, item.resource) for item in self.unmatched]
        for group in self.ambiguous:
            found += [(ComparisonSide.LEFT, ref) for ref in group.left]
            found += [(ComparisonSide.RIGHT, ref) for ref in group.right]
        return found

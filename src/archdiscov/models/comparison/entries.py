"""Entries of a snapshot comparison: matched pairs, unmatched resources, ambiguous matches.

Every semantic field is part of an entry's deterministic ID and every list is in canonical
order. Entries report recorded differences and qualified absences: never proof that a
resource was added, removed, or drifted.
"""

from collections.abc import Callable, Sequence
from typing import Self

from pydantic import Field, JsonValue, model_validator

from ..analysis.references import CoverageReference, ResourceReference
from ..canonical import canonical_json, sha256_json
from ..enums import (
    ComparisonSide,
    DifferenceKind,
    DifferenceSubject,
    MatchBasis,
    QualificationKind,
)
from ..resource_base import FactPath, Model, NonEmptyString, ResourceLifecycle


def require_canonical[T](items: Sequence[T], key: Callable[[T], object], label: str) -> None:
    """Items are unique and sorted by `key`: one canonical form for deterministic IDs."""
    keys = [canonical_json(key(item)) for item in items]
    if keys != sorted(set(keys)):
        raise ValueError(f"{label} must be unique and in canonical order")


def require_sorted_ids(ids: Sequence[str]) -> None:
    if list(ids) != sorted(set(ids)):
        raise ValueError("evidence_ids must be unique and sorted")


class Qualification(Model):
    """Why a statement is qualified: planned lifecycle, limited coverage, or an unusable value.

    `side` is the snapshot the qualification concerns. A planned lifecycle carries the plan's
    action sequence (a planned deletion is not an observed absence), limited coverage names the
    recorded operation, and unrecorded coverage names only the service, never an operation.
    """

    kind: QualificationKind
    side: ComparisonSide
    coverage: CoverageReference | None = None
    service: NonEmptyString | None = None
    lifecycle: ResourceLifecycle | None = None
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> Self:
        required = {
            QualificationKind.PLANNED_LIFECYCLE: "lifecycle",
            QualificationKind.LIMITED_COVERAGE: "coverage",
            QualificationKind.UNRECORDED_COVERAGE: "service",
        }.get(self.kind)
        for name in ("coverage", "service", "lifecycle"):
            expected = name == required
            if (getattr(self, name) is not None) != expected:
                verb = "require" if expected else "forbid"
                raise ValueError(f"{self.kind} qualifications {verb} {name}")
        require_sorted_ids(self.evidence_ids)
        return self


def _sorted_qualifications(items: list[Qualification]) -> None:
    require_canonical(items, lambda item: item.model_dump(mode="json"), "qualifications")


class FieldDifference(Model):
    """One recorded difference of a matched pair, cited on both sides.

    `path` is a JSON pointer into the subject (the resource document, or a dependency or
    containment key). The value of the side where the path is absent is None, so a present JSON
    null is distinguished by the kind. An indeterminate difference carries no values and needs a
    qualification saying why it cannot be compared.
    """

    subject: DifferenceSubject = DifferenceSubject.FIELD
    path: FactPath
    kind: DifferenceKind
    left_value: JsonValue = None
    right_value: JsonValue = None
    left_evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    right_evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    qualifications: list[Qualification] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_difference(self) -> Self:
        if self.kind == DifferenceKind.INDETERMINATE:
            if self.left_value is not None or self.right_value is not None:
                raise ValueError("indeterminate differences carry no values")
            if not self.qualifications:
                raise ValueError("indeterminate differences require a qualification")
        elif self.kind == DifferenceKind.ADDED and self.left_value is not None:
            raise ValueError("an added field has no left value")
        elif self.kind == DifferenceKind.REMOVED and self.right_value is not None:
            raise ValueError("a removed field has no right value")
        elif self.kind == DifferenceKind.CHANGED and self.left_value == self.right_value:
            raise ValueError("a changed field must have different values")
        require_sorted_ids(self.left_evidence_ids)
        require_sorted_ids(self.right_evidence_ids)
        _sorted_qualifications(self.qualifications)
        return self


class MatchedPair(Model):
    """Two resources paired by identity, with their field differences and qualifications."""

    id: NonEmptyString = Field(default="")
    left: ResourceReference
    right: ResourceReference
    basis: MatchBasis
    differences: list[FieldDifference] = Field(default_factory=list)
    qualifications: list[Qualification] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.left.scope != self.right.scope:
            raise ValueError("matched resources must share a complete scope")
        require_canonical(self.differences, lambda d: (d.subject, d.path, d.kind), "differences")
        _sorted_qualifications(self.qualifications)
        return _with_derived_id(self)


_ABSENCE_QUALIFICATIONS = {
    QualificationKind.PLANNED_LIFECYCLE,
    QualificationKind.LIMITED_COVERAGE,
    QualificationKind.UNRECORDED_COVERAGE,
}


class UnmatchedResource(Model):
    """A resource with no counterpart on the other side (qualified, never a proven removal)."""

    id: NonEmptyString = Field(default="")
    side: ComparisonSide
    resource: ResourceReference
    qualifications: list[Qualification] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unmatched(self) -> Self:
        if any(item.side != self.side for item in self.qualifications):
            raise ValueError("qualifications of an unmatched resource concern its own side")
        if any(item.kind not in _ABSENCE_QUALIFICATIONS for item in self.qualifications):
            raise ValueError("an unmatched resource is qualified by lifecycle or coverage only")
        _sorted_qualifications(self.qualifications)
        return _with_derived_id(self)


class AmbiguousMatch(Model):
    """Several candidates share one match key: all are listed and none is chosen."""

    id: NonEmptyString = Field(default="")
    basis: MatchBasis
    key: NonEmptyString
    left: list[ResourceReference] = Field(min_length=1)
    right: list[ResourceReference] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ambiguity(self) -> Self:
        if len(self.left) + len(self.right) < 3:
            raise ValueError("an ambiguous match needs more than one candidate on a side")
        for candidates in (self.left, self.right):
            require_canonical(candidates, lambda ref: ref.resource_id, "candidates")
        return _with_derived_id(self)


def _with_derived_id[E: (MatchedPair, UnmatchedResource, AmbiguousMatch)](entry: E) -> E:
    derived = sha256_json(entry.model_dump(mode="json", exclude={"id"}))
    if entry.id and entry.id != derived:
        raise ValueError("entry id must match its deterministic derivation")
    entry.id = derived
    return entry

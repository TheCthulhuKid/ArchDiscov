"""Compare the recorded fields of matched resources, without calling a difference drift.

For each matched pair this reports differences in the resource document (typed fields,
attributes, tags), the containment parent, and the outgoing dependencies, each as a JSON-pointer
path, a kind, both values, and the evidence cited on each side. Identifiers that differ only by
snapshot (the resource ID and identity), evidence bookkeeping, the children (compared as their
own resources), the planned lifecycle (qualified separately), and the Terraform type name that
only a Terraform import records are not compared.

Values that cannot be compared are indeterminate, never differences: a redacted value is not
compared by content, a field absent from (or null and uncited on) a side that recorded unknown
or omitted values may be one of them, and a dependency group holding an unresolved reference
cannot be judged against the other side's targets. Dependency targets are compared by matched
identity, so two edges to different resources that match each other are equal. Lists are
compared by position.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import JsonValue

from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.canonical import canonical_json
from archdiscov.models.comparison.entries import FieldDifference, Qualification
from archdiscov.models.edges import DependencyEdge
from archdiscov.models.enums import (
    ComparisonSide,
    DifferenceKind,
    DifferenceSubject,
    EvidenceValueStatus,
    QualificationKind,
)
from archdiscov.models.json_pointer import escape_pointer_token
from archdiscov.models.sanitization import REDACTED_MARKER

from .comparison_matching import MatchedIds

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

LEFT, RIGHT = ComparisonSide.LEFT, ComparisonSide.RIGHT
_NOT_COMPARED = {"id", "identity", "evidence_ids", "fact_evidence", "children", "lifecycle"}
# Source-specific bookkeeping that differs by how a resource was recorded, not by the resource.
_BOOKKEEPING = frozenset({"/attributes/terraform_type"})
_UNAVAILABLE = {EvidenceValueStatus.UNRESOLVED, EvidenceValueStatus.OMITTED}
_MISSING = object()
_PARENT_PATH = "/parent"

Counterpart = tuple[str | None, str | None]  # (left ID, right ID); None where unmatched


@dataclass
class _Edges:
    """One side's outgoing dependencies in one relationship, attachment, and location group."""

    targets: dict[str, tuple[JsonValue, set[str]]] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)  # evidence of unresolved references


def _walk(left: object, right: object, path: str, found: list[tuple[str, object, object]]) -> None:
    """Collect the paths where two JSON documents differ, descending through shared containers."""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys()):
            child = f"{path}/{escape_pointer_token(key)}"
            _walk(left.get(key, _MISSING), right.get(key, _MISSING), child, found)
    elif isinstance(left, list) and isinstance(right, list):
        for position in range(max(len(left), len(right))):
            first = left[position] if position < len(left) else _MISSING
            second = right[position] if position < len(right) else _MISSING
            _walk(first, second, f"{path}/{position}", found)
    elif left != right:
        found.append((path, left, right))


def _value(item: object) -> JsonValue:
    return None if item is _MISSING else item  # type: ignore[return-value]


class FieldComparer:
    """Field-level differences of matched pairs of two indexes (build once per comparison)."""

    def __init__(self, left: GraphIndex, right: GraphIndex, matched: Iterable[MatchedIds]) -> None:
        self._index = {LEFT: left, RIGHT: right}
        pairs = tuple(matched)
        self._counterpart = {
            LEFT: {pair.left_id: pair.right_id for pair in pairs},
            RIGHT: {pair.right_id: pair.left_id for pair in pairs},
        }

    def compare(self, pair: MatchedIds) -> list[FieldDifference]:
        """Every recorded difference of one matched pair, unique and in canonical order."""
        ids = {LEFT: pair.left_id, RIGHT: pair.right_id}
        found = [
            *self._fields(ids),
            *self._containment(ids),
            *self._dependencies(ids),
        ]
        return sorted(found, key=lambda d: canonical_json((d.subject, d.path, d.kind)))

    # ---- shared helpers ------------------------------------------------------------

    def _counterparts(self, side: ComparisonSide, resource_id: str) -> Counterpart:
        other = self._counterpart[side].get(resource_id)
        return (resource_id, other) if side == LEFT else (other, resource_id)

    def _unavailable(self, side: ComparisonSide, resource_id: str) -> list[str]:
        """Evidence of values the source could not provide (unknown or omitted)."""
        index = self._index[side]
        return [
            evidence_id
            for evidence_id in index.resource_evidence_ids(resource_id)
            if (record := index.get_evidence(evidence_id)) is not None
            and record.value_status in _UNAVAILABLE
        ]

    def _fact_citation(self, side: ComparisonSide, resource_id: str, pointer: str) -> list[str]:
        """Citations of the recorded fact at or above `pointer`; none if nothing is cited."""
        index = self._index[side]
        while pointer:
            if ids := index.fact_evidence_ids(resource_id, pointer):
                return sorted(set(ids))
            pointer = pointer.rpartition("/")[0]
        return []

    def _cited(self, side: ComparisonSide, resource_id: str, pointer: str) -> list[str]:
        """The fact's citations, else the resource's own evidence (never an invented one)."""
        cited = self._fact_citation(side, resource_id, pointer)
        return cited or sorted(set(self._index[side].resource_evidence_ids(resource_id)))

    def _difference(
        self,
        ids: dict[ComparisonSide, str],
        subject: DifferenceSubject,
        path: str,
        kind: DifferenceKind,
        values: tuple[object, object] = (_MISSING, _MISSING),
        evidence: dict[ComparisonSide, Iterable[str]] | None = None,
        qualification: Qualification | None = None,
    ) -> FieldDifference:
        """A difference citing `evidence` per side where given, else the recorded fact."""
        evidence = evidence or {}
        cited = {
            side: sorted(set(evidence[side]))
            if side in evidence
            else self._cited(side, ids[side], path)
            for side in (LEFT, RIGHT)
        }
        indeterminate = kind == DifferenceKind.INDETERMINATE
        return FieldDifference(
            subject=subject,
            path=path,
            kind=kind,
            left_value=None if indeterminate else _value(values[0]),
            right_value=None if indeterminate else _value(values[1]),
            left_evidence_ids=cited[LEFT],
            right_evidence_ids=cited[RIGHT],
            qualifications=[qualification] if qualification else [],
        )

    # ---- resource document ---------------------------------------------------------

    def _document(self, side: ComparisonSide, resource_id: str) -> dict[str, JsonValue]:
        resource = self._index[side].get_resource(resource_id)
        if resource is None:
            raise ValueError("matched IDs come from the same indexes")
        return resource.model_dump(mode="json", exclude=_NOT_COMPARED)

    def _fields(self, ids: dict[ComparisonSide, str]) -> list[FieldDifference]:
        found: list[tuple[str, object, object]] = []
        _walk(self._document(LEFT, ids[LEFT]), self._document(RIGHT, ids[RIGHT]), "", found)
        found = [item for item in found if item[0] not in _BOOKKEEPING]
        if not found:
            return []
        unavailable = {side: self._unavailable(side, ids[side]) for side in (LEFT, RIGHT)}
        return [self._field_difference(ids, unavailable, *item) for item in found]

    def _field_difference(
        self,
        ids: dict[ComparisonSide, str],
        unavailable: dict[ComparisonSide, list[str]],
        path: str,
        left: object,
        right: object,
    ) -> FieldDifference:
        values = {LEFT: left, RIGHT: right}
        redacted = [side for side in (LEFT, RIGHT) if values[side] == REDACTED_MARKER]
        if redacted:  # a redacted value is never compared by content
            side = redacted[0]
            return self._indeterminate(
                ids,
                DifferenceSubject.FIELD,
                path,
                QualificationKind.REDACTED_VALUE,
                side,
                self._cited(side, ids[side], path),
            )
        for side in (LEFT, RIGHT):
            if unavailable[side] and self._may_be_unavailable(side, ids[side], path, values[side]):
                return self._indeterminate(
                    ids,
                    DifferenceSubject.FIELD,
                    path,
                    QualificationKind.UNKNOWN_VALUE,
                    side,
                    unavailable[side],
                )
        kind = _change_kind(left is _MISSING, right is _MISSING)
        return self._difference(ids, DifferenceSubject.FIELD, path, kind, (left, right))

    def _may_be_unavailable(
        self, side: ComparisonSide, resource_id: str, path: str, value: object
    ) -> bool:
        """Whether a side's missing or null value may be a value the source could not provide.

        An absent field may be one. So may a null that nothing cites: a typed field of a plan
        resource with an unknown value is stored as null, whereas a known null has a citation.
        """
        if value is _MISSING:
            return True
        return value is None and not self._fact_citation(side, resource_id, path)

    def _indeterminate(
        self,
        ids: dict[ComparisonSide, str],
        subject: DifferenceSubject,
        path: str,
        kind: QualificationKind,
        side: ComparisonSide,
        evidence: Iterable[str],
        cited: dict[ComparisonSide, Iterable[str]] | None = None,
    ) -> FieldDifference:
        qualification = Qualification(kind=kind, side=side, evidence_ids=sorted(set(evidence)))
        return self._difference(
            ids,
            subject,
            path,
            DifferenceKind.INDETERMINATE,
            evidence=cited,
            qualification=qualification,
        )

    # ---- containment ---------------------------------------------------------------

    def _parent(self, side: ComparisonSide, resource_id: str) -> Counterpart | None:
        parent_id = self._index[side].parent_id(resource_id)
        return None if parent_id is None else self._counterparts(side, parent_id)

    def _containment(self, ids: dict[ComparisonSide, str]) -> list[FieldDifference]:
        left, right = self._parent(LEFT, ids[LEFT]), self._parent(RIGHT, ids[RIGHT])
        if left == right:
            return []
        kind = _change_kind(left is None, right is None)
        values = (_counterpart_json(left), _counterpart_json(right))
        return [self._difference(ids, DifferenceSubject.CONTAINMENT, _PARENT_PATH, kind, values)]

    # ---- dependencies --------------------------------------------------------------

    def _edge_groups(self, side: ComparisonSide, resource_id: str) -> dict[tuple[str, ...], _Edges]:
        """Outgoing edges by (relationship, attachment kind, location); targets by identity."""
        groups: dict[tuple[str, ...], _Edges] = {}
        for edge in self._index[side].outgoing_edges(resource_id):
            group = groups.setdefault(_group_key(edge), _Edges())
            evidence = set(edge.evidence_ids)
            if isinstance(edge.target, UnresolvedTarget):
                group.unresolved |= evidence
            elif isinstance(edge.target, LocalTarget):
                pair = self._counterparts(side, edge.target.resource_id)
                group.targets.setdefault(canonical_json(pair), (_counterpart_json(pair), set()))[
                    1
                ].update(evidence)
            else:
                value = edge.target.model_dump(mode="json")
                group.targets.setdefault(canonical_json(value), (value, set()))[1].update(evidence)
        return groups

    def _dependencies(self, ids: dict[ComparisonSide, str]) -> list[FieldDifference]:
        edges = {side: self._edge_groups(side, ids[side]) for side in (LEFT, RIGHT)}
        found: list[FieldDifference] = []
        for group in sorted(edges[LEFT].keys() | edges[RIGHT].keys()):
            sides = {side: edges[side].get(group, _Edges()) for side in (LEFT, RIGHT)}
            found.extend(self._group_differences(ids, group, sides))
        return found

    def _group_differences(
        self,
        ids: dict[ComparisonSide, str],
        group: tuple[str, ...],
        sides: dict[ComparisonSide, _Edges],
    ) -> list[FieldDifference]:
        found: list[FieldDifference] = []
        absorbed = {LEFT: False, RIGHT: False}  # an unresolved side explains the other's extras
        for side in (LEFT, RIGHT):
            other = side.opposite
            for key in sorted(sides[side].targets.keys() - sides[other].targets.keys()):
                value, evidence = sides[side].targets[key]
                path = _dependency_path(group, key)
                if sides[other].unresolved:
                    absorbed[other] = True
                    found.append(
                        self._indeterminate(
                            ids,
                            DifferenceSubject.DEPENDENCY,
                            path,
                            QualificationKind.UNRESOLVED_REFERENCE,
                            other,
                            sides[other].unresolved,
                            {side: evidence, other: sides[other].unresolved},
                        )
                    )
                    continue
                values = (value, _MISSING) if side == LEFT else (_MISSING, value)
                kind = _change_kind(side == RIGHT, side == LEFT)
                found.append(
                    self._difference(
                        ids, DifferenceSubject.DEPENDENCY, path, kind, values, {side: evidence}
                    )
                )
        for side in (LEFT, RIGHT):  # an unresolved reference with nothing on the other side
            other = side.opposite
            if sides[side].unresolved and not sides[other].unresolved and not absorbed[side]:
                found.append(
                    self._indeterminate(
                        ids,
                        DifferenceSubject.DEPENDENCY,
                        _dependency_path(group, None),
                        QualificationKind.UNRESOLVED_REFERENCE,
                        side,
                        sides[side].unresolved,
                        {side: sides[side].unresolved},
                    )
                )
        return found


def _change_kind(left_absent: bool, right_absent: bool) -> DifferenceKind:
    if left_absent:
        return DifferenceKind.ADDED
    return DifferenceKind.REMOVED if right_absent else DifferenceKind.CHANGED


def _counterpart_json(pair: Counterpart | None) -> JsonValue:
    return None if pair is None else {"left": pair[0], "right": pair[1]}


def _group_key(edge: DependencyEdge) -> tuple[str, ...]:
    kind = edge.attachment_kind
    return (str(edge.relationship), "" if kind is None else str(kind), edge.location or "")


def _dependency_path(group: tuple[str, ...], target_key: str | None) -> str:
    tokens = group if target_key is None else (*group, target_key)
    return "".join(f"/{escape_pointer_token(token)}" for token in tokens)

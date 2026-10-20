"""Pair the resources of two snapshots by identity, without guessing.

Resources match only inside one complete scope (the same cloud, account or subscription and
partition or tenant): equal names or short IDs in other scopes or clouds never imply an
equivalent resource. A resource with a physical identity matches on its native type and its
scope-qualified physical identity (Azure ARM IDs and types compare case-insensitively, and
only Azure's do); it never falls back to a logical address or a name. A resource without one
matches its logical address (including a Terraform module address) only when both snapshots
come from the same IaC context. When several resources of a side share one key, every
candidate is reported as ambiguous and none is chosen. Matching is symmetric apart from the
side labels, deterministic, and linear in the resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from archdiscov.graph.identity import native_resource_type
from archdiscov.models.enums import (
    CloudPlatform,
    ComparisonLimitationKind,
    MatchBasis,
    SnapshotSourceKind,
)
from archdiscov.models.resource_base import Resource

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_IAC_CONTEXTS = {
    SnapshotSourceKind.TERRAFORM_STATE: "terraform",
    SnapshotSourceKind.TERRAFORM_PLAN: "terraform",
    SnapshotSourceKind.CLOUDFORMATION: "cloudformation",
}
MatchKey = tuple[MatchBasis, str, str, str]  # basis, native type, identity, IaC context
Groups = dict[MatchKey, tuple[str, list[str]]]  # key -> readable identifier, resource IDs


@dataclass(frozen=True)
class MatchedIds:
    left_id: str
    right_id: str
    basis: MatchBasis


@dataclass(frozen=True)
class AmbiguousIds:
    basis: MatchBasis
    key: str
    left_ids: tuple[str, ...]
    right_ids: tuple[str, ...]


@dataclass(frozen=True)
class MatchOutcome:
    """Resource IDs only; the comparison service turns them into contract entries."""

    matched: tuple[MatchedIds, ...]
    unmatched_left: tuple[str, ...]
    unmatched_right: tuple[str, ...]
    ambiguous: tuple[AmbiguousIds, ...]
    limitations: tuple[ComparisonLimitationKind, ...]


def iac_context(kind: SnapshotSourceKind) -> str | None:
    """The namespace of logical addresses, or None for sources that have none."""
    return _IAC_CONTEXTS.get(kind)


def _native_type(resource: Resource) -> str:
    native = native_resource_type(resource)
    return native.lower() if resource.identity.platform == CloudPlatform.AZURE else native


def match_key(resource: Resource, context: str | None) -> tuple[MatchKey, str] | None:
    """The key a resource matches on, and the readable identifier shown when it is ambiguous.

    A physical identity is the stronger key and is never replaced by a logical one. A logical
    address is a key only in an IaC context, so observed resources without a physical
    identity are never matched.
    """
    identity = resource.identity
    native = _native_type(resource)
    if identity.physical_id is not None:
        shown = (
            identity.canonical_arm_id
            if identity.platform == CloudPlatform.AZURE
            else identity.physical_id
        )
        return (MatchBasis.PHYSICAL_IDENTITY, native, identity.stable_id, ""), f"{native} {shown}"
    if context is None:
        return None
    key = (MatchBasis.LOGICAL_ADDRESS, native, identity.stable_id, context)
    return key, f"{native} {identity.logical_address}"


def _group(index: GraphIndex, context: str | None) -> Groups:
    groups: Groups = {}
    for resource in index.resources():  # in ID order, so every group is sorted
        keyed = match_key(resource, context)
        if keyed is not None:
            key, shown = keyed
            groups.setdefault(key, (shown, []))[1].append(resource.id)
    return groups


def _incompatibility(left: GraphIndex, right: GraphIndex) -> ComparisonLimitationKind | None:
    if left.scope == right.scope:
        return None
    if left.scope.platform != right.scope.platform:
        return ComparisonLimitationKind.CROSS_CLOUD
    return ComparisonLimitationKind.SCOPE_MISMATCH


def pair_groups(left: Groups, right: Groups) -> tuple[list[MatchedIds], list[AmbiguousIds]]:
    """Pair keys present on both sides: one candidate each is a match, anything else ambiguous.

    Every candidate of an ambiguous key is listed and none is chosen; a key on one side only
    pairs nothing.
    """
    matched: list[MatchedIds] = []
    ambiguous: list[AmbiguousIds] = []
    for key in sorted(left.keys() & right.keys()):
        shown, left_ids = left[key]
        right_ids = right[key][1]
        if len(left_ids) == len(right_ids) == 1:
            matched.append(MatchedIds(left_ids[0], right_ids[0], key[0]))
        else:
            ambiguous.append(AmbiguousIds(key[0], shown, tuple(left_ids), tuple(right_ids)))
    return matched, ambiguous


def match_resources(left: GraphIndex, right: GraphIndex) -> MatchOutcome:
    """Pair the two snapshots' resources; scopes that differ in any way match nothing."""
    if (reason := _incompatibility(left, right)) is not None:
        return MatchOutcome((), left.resource_ids, right.resource_ids, (), (reason,))
    left_context, right_context = iac_context(left.source_kind), iac_context(right.source_kind)
    # Logical addresses are comparable only inside one IaC namespace.
    context = left_context if left_context == right_context else None
    left_groups, right_groups = _group(left, context), _group(right, context)
    matched, ambiguous = pair_groups(left_groups, right_groups)
    paired = left_groups.keys() & right_groups.keys()
    claimed_left = {i for key in paired for i in left_groups[key][1]}
    claimed_right = {i for key in paired for i in right_groups[key][1]}
    return MatchOutcome(
        matched=tuple(sorted(matched, key=lambda item: (item.left_id, item.right_id))),
        unmatched_left=tuple(i for i in left.resource_ids if i not in claimed_left),
        unmatched_right=tuple(i for i in right.resource_ids if i not in claimed_right),
        ambiguous=tuple(sorted(ambiguous, key=lambda item: (item.basis, item.key))),
        limitations=(),
    )

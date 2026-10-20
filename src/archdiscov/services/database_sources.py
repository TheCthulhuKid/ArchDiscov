"""Candidate compute sources in the database's VPC, with their recorded placement.

A candidate is a placement, never evidence of usage: nothing here infers which compute
resource actually talks to the database. Each source is described by interfaces (a subnet,
security groups, and the private IPv4 addresses recorded for it); evaluation treats the
interfaces as alternatives. Sources whose placement cannot be established stay candidates
with limitations, and sources in other VPCs are unsupported and only counted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from ipaddress import IPv4Address
from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.architecture import Ec2Instance, Subnet, Vpc
from archdiscov.models.enums import AccessSourceKind, AttachmentKind
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.ecs import EcsTask, EcsTaskDefinition
from archdiscov.models.resources.functions import LambdaFunction
from archdiscov.models.resources.network import NetworkInterface

from .database_placement import DatabasePlacement
from .placement_collection import PlacementCollector, limitation

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

ComputeSource = Ec2Instance | EcsTask | LambdaFunction
_KINDS = {
    Ec2Instance: AccessSourceKind.EC2_INSTANCE,
    EcsTask: AccessSourceKind.ECS_TASK,
    LambdaFunction: AccessSourceKind.LAMBDA_FUNCTION,
}


class SourceStatus(StrEnum):
    PLACED = "placed"  # at least one interface with a subnet and security groups
    INCOMPLETE = "incomplete"  # placement cannot be established: insufficient evidence


@dataclass(frozen=True)
class SourceInterface:
    """One alternative attachment point of a source; IDs are stored resource IDs."""

    subnet_id: str
    security_group_ids: tuple[str, ...]
    addresses: tuple[IPv4Address, ...]


@dataclass(frozen=True)
class CandidateSource:
    resource: ComputeSource
    kind: AccessSourceKind
    status: SourceStatus
    interfaces: tuple[SourceInterface, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[AnalysisLimitation, ...]


@dataclass(frozen=True)
class SourceDiscovery:
    """Bounded candidates plus what could not be listed; `total` counts all candidates."""

    candidates: tuple[CandidateSource, ...]
    total: int
    other_vpc_ids: tuple[str, ...]
    limitations: tuple[AnalysisLimitation, ...]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.candidates)


def _vpc_of(index: GraphIndex, resource_id: str) -> str | None:
    """The stored VPC containing the resource (itself, or the nearest ancestor)."""
    current: str | None = resource_id
    while current is not None:
        if isinstance(index.get_resource(current), Vpc):
            return current
        current = index.parent_id(current)
    return None


def _ipv4(value: object) -> IPv4Address | None:
    try:
        return IPv4Address(value)  # type: ignore[arg-type]
    except ValueError:
        return None


def _addresses(
    resources: list[NetworkInterface], collector: PlacementCollector
) -> tuple[IPv4Address, ...]:
    found: set[IPv4Address] = set()
    for interface in resources:
        citations = interface.fact_evidence.get("/private_ip_addresses", [])
        collector.evidence.update(citations)
        for entry in interface.private_ip_addresses:
            if (address := _ipv4(entry.address)) is not None:
                found.add(address)
            else:
                collector.note(
                    limitation(
                        "source_address_unsupported",
                        "A recorded private address is not an IPv4 address.",
                        citations,
                    )
                )
    return tuple(sorted(found))


def _instance_interfaces_of(
    index: GraphIndex, instance: Ec2Instance, collector: PlacementCollector
) -> list[NetworkInterface]:
    """Stored interfaces attached to the instance (the interfaces point at it)."""
    found = []
    for edge in index.incoming_edges(
        instance.id, attachment_kind=AttachmentKind.INSTANCE_INTERFACE
    ):
        collector.evidence.update(edge.evidence_ids)
        found.append(index.get_resource(edge.source_id))
    return _sorted_interfaces(found)


def _task_interfaces_of(
    index: GraphIndex, task: EcsTask, collector: PlacementCollector
) -> list[NetworkInterface]:
    """Stored interfaces the task points at; unresolved ones become limitations."""
    found = [
        index.get_resource(edge.target_id)
        for edge in index.outgoing_edges(task.id, attachment_kind=AttachmentKind.INSTANCE_INTERFACE)
        if collector.cite_edge(edge, "network interface")
    ]
    return _sorted_interfaces(found)


def _sorted_interfaces(found: list[Resource | None]) -> list[NetworkInterface]:
    return sorted(
        (item for item in found if isinstance(item, NetworkInterface)), key=lambda i: i.id
    )


def _security_groups(owner: Resource, collector: PlacementCollector) -> set[str]:
    groups = collector.memberships(
        [owner], AttachmentKind.SECURITY_GROUP_MEMBERSHIP, "security group"
    )
    if not groups:
        collector.note(
            limitation("source_security_groups_unrecorded", "No security group is recorded.")
        )
    return groups


def _note_no_address(collector: PlacementCollector) -> None:
    collector.note(limitation("source_address_unrecorded", "No private IPv4 is recorded."))


def _subnet_of(index: GraphIndex, resource: Resource) -> str | None:
    parent = index.parent_id(resource.id)
    return parent if isinstance(index.get_resource(parent or ""), Subnet) else None


def interface_placement(
    index: GraphIndex, eni: NetworkInterface, collector: PlacementCollector
) -> SourceInterface | None:
    subnet = _subnet_of(index, eni)
    groups = collector.memberships(
        [eni], AttachmentKind.SECURITY_GROUP_MEMBERSHIP, "security group"
    )
    if subnet is None or not groups:
        collector.note(
            limitation(
                "source_interface_incomplete",
                f"Interface {eni.identity.physical_id} has no recorded subnet or security group.",
                index.resource_evidence_ids(eni.id),
            )
        )
        return None
    collector.evidence.update(eni.evidence_ids)
    return SourceInterface(subnet, tuple(sorted(groups)), _addresses([eni], collector))


def instance_placement(
    index: GraphIndex, instance: Ec2Instance, collector: PlacementCollector
) -> list[SourceInterface]:
    enis = _instance_interfaces_of(index, instance, collector)
    if enis:
        found = [interface_placement(index, eni, collector) for eni in enis]
        return [item for item in found if item is not None]
    subnet = _subnet_of(index, instance)
    collector.evidence.update(instance.fact_evidence.get("/attributes/subnet_id", []))
    if subnet is None:
        collector.note(limitation("source_subnets_unrecorded", "No subnet is recorded."))
    groups = _security_groups(instance, collector)
    if subnet is None or not groups:
        return []
    collector.evidence.update(instance.fact_evidence.get("/attributes/private_ip_address", []))
    address = _ipv4(instance.attributes.get("private_ip_address"))
    if address is None:
        _note_no_address(collector)
    return [SourceInterface(subnet, tuple(sorted(groups)), (address,) if address else ())]


def _subnet_interfaces(
    index: GraphIndex,
    owner: ComputeSource,
    collector: PlacementCollector,
    addresses_by_subnet: dict[str, tuple[IPv4Address, ...]],
) -> list[SourceInterface]:
    """One interface per recorded subnet for tasks and functions (membership edges)."""
    subnets = collector.memberships([owner], AttachmentKind.SUBNET_MEMBERSHIP, "subnet")
    groups = _security_groups(owner, collector)
    if not subnets:
        code = (
            "source_not_in_vpc"
            if isinstance(owner, LambdaFunction)
            else "source_subnets_unrecorded"
        )
        collector.note(limitation(code, "No VPC subnet placement is recorded."))
    if not subnets or not groups:
        return []
    interfaces = [
        SourceInterface(subnet, tuple(sorted(groups)), addresses_by_subnet.get(subnet, ()))
        for subnet in sorted(subnets)
    ]
    if not any(item.addresses for item in interfaces):
        _note_no_address(collector)
    return interfaces


def _task_interfaces(
    index: GraphIndex, task: EcsTask, collector: PlacementCollector
) -> list[SourceInterface]:
    modes = {
        definition.network_mode
        for edge in index.outgoing_edges(task.id, attachment_kind=AttachmentKind.TASK_DEFINITION)
        if isinstance(definition := index.get_resource(edge.target_id), EcsTaskDefinition)
    }
    if modes - {None, "awsvpc"}:
        collector.note(limitation("source_not_awsvpc", "The task does not use awsvpc networking."))
        return []
    by_subnet: dict[str, tuple[IPv4Address, ...]] = {}
    for eni in _task_interfaces_of(index, task, collector):
        subnet = _subnet_of(index, eni)
        if subnet is not None:
            by_subnet[subnet] = tuple(
                sorted({*by_subnet.get(subnet, ()), *_addresses([eni], collector)})
            )
    return _subnet_interfaces(index, task, collector, by_subnet)


def _describe(index: GraphIndex, resource: ComputeSource) -> CandidateSource:
    collector = PlacementCollector(index)
    if isinstance(resource, Ec2Instance):
        interfaces = instance_placement(index, resource, collector)
    elif isinstance(resource, EcsTask):
        interfaces = _task_interfaces(index, resource, collector)
    else:
        interfaces = _subnet_interfaces(index, resource, collector, {})
    return CandidateSource(
        resource=resource,
        kind=_KINDS[type(resource)],
        status=SourceStatus.PLACED if interfaces else SourceStatus.INCOMPLETE,
        interfaces=tuple(interfaces),
        evidence_ids=tuple(sorted(collector.evidence)),
        limitations=collector.sorted_limitations(),
    )


def _in_vpc(index: GraphIndex, candidate: CandidateSource, vpc_id: str) -> bool | None:
    """Whether the candidate touches the VPC; None when its VPC cannot be established."""
    anchors = [item.subnet_id for item in candidate.interfaces] or [candidate.resource.id]
    vpcs = {_vpc_of(index, anchor) for anchor in anchors}
    return None if None in vpcs else vpc_id in vpcs


def _restrict_to_vpc(index: GraphIndex, candidate: CandidateSource, vpc_id: str) -> CandidateSource:
    """Keep only the interfaces inside the database VPC."""
    inside = tuple(
        item for item in candidate.interfaces if _vpc_of(index, item.subnet_id) == vpc_id
    )
    return replace(candidate, interfaces=inside)


def _sort_key(candidate: CandidateSource) -> tuple[int, int, str]:
    return (
        list(SourceStatus).index(candidate.status),
        list(AccessSourceKind).index(candidate.kind),
        candidate.resource.id,
    )


def discover_sources(
    index: GraphIndex, placement: DatabasePlacement, result_limit: int | None = None
) -> SourceDiscovery:
    """Candidates in the database's VPC, ordered by status, kind, and ID.

    The result limit bounds the listed candidates (None lists them all). Without a resolved
    database VPC nothing can be placed, so no candidate is listed and a limitation says why.
    Sources in other VPCs are unsupported: they are counted, not listed.
    """
    if placement.vpc_id is None:
        reason = "Candidate sources cannot be listed without the database's VPC."
        return SourceDiscovery((), 0, (), (limitation("sources_unfiltered", reason),))
    candidates: list[CandidateSource] = []
    other: list[str] = []
    for resource in index.resources():
        if not isinstance(resource, tuple(_KINDS)):
            continue
        candidate = _describe(index, resource)
        inside = _in_vpc(index, candidate, placement.vpc_id)
        if inside is False:
            other.append(resource.id)
        else:
            candidates.append(
                _restrict_to_vpc(index, candidate, placement.vpc_id) if inside else candidate
            )
    ordered = sorted(candidates, key=_sort_key)
    limitations = ()
    if other:
        message = f"{len(other)} compute source(s) in other VPCs are unsupported."
        limitations = (limitation("sources_other_vpc", message),)
    return SourceDiscovery(
        tuple(ordered[:result_limit]), len(ordered), tuple(sorted(other)), limitations
    )

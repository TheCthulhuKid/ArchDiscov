"""Candidate internet exposure of resources with a recorded public IPv4 address.

An entry point is a network interface or an instance without stored interfaces whose public
address is recorded (or unknown). The ingress ports come from security-group rules open to
`0.0.0.0/0`; each port range is split where other rules or ACL entries start or stop, and the
pieces are composed: the public address, the subnet's route to an attached internet gateway
(for one documented representative public address), the ACLs for the request and the return
path, and the security-group ingress. Resources that merely share a subnet with a public one
are never entries. Everything is recorded configuration, not proof of reachability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.architecture import Ec2Instance
from archdiscov.models.enums import (
    AttachmentKind,
    ExposureElement,
    ExposureEntryKind,
    ExposureProtocol,
    RouteReach,
    RuleDirection,
)
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import (
    NaclEntry,
    NetworkAcl,
    NetworkInterface,
    SecurityGroup,
)

from .database_sources import SourceInterface, instance_placement, interface_placement
from .exposure_evaluation import ExposureEvaluation, evaluate_elements, merge_adjacent
from .exposure_ports import MAX_PORT, MIN_PORT, PortRange, boundaries, internet_open_ports, split
from .network_addressing import evaluate_public_address, recorded_public_addresses
from .network_composition import groups_allow, nacl_round_trip
from .network_decisions import any_of, missing, unsupported
from .network_matching import implicit_deny, sole_associate
from .network_probes import INTERNET_CIDR, PROBE_ADDRESS
from .network_routes import evaluate_subnet_route
from .network_security_groups import Peer
from .placement_collection import PlacementCollector

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

Entry = NetworkInterface | Ec2Instance
_WHOLE_RANGE = (MIN_PORT, MAX_PORT)
_UNNEEDED = frozenset({"source_address_unrecorded"})  # exposure does not need the private address
_INGRESS, _EGRESS = RuleDirection.INGRESS, RuleDirection.EGRESS
_E = ExposureElement


def public_entries(index: GraphIndex) -> list[Entry]:
    """Interfaces and interface-less instances with a recorded public address.

    An instance with stored interfaces is represented by them. Limited address collection does
    not turn every resource into an entry: only a recorded address does. The order is
    deterministic.
    """
    interfaced = {
        edge.target_id
        for interface in index.resources()
        if isinstance(interface, NetworkInterface)
        for edge in index.outgoing_edges(
            interface.id, attachment_kind=AttachmentKind.INSTANCE_INTERFACE
        )
    }
    return sorted(
        (
            resource
            for resource in index.resources()
            if isinstance(resource, NetworkInterface | Ec2Instance)
            and resource.id not in interfaced
            and recorded_public_addresses(resource)[0]
        ),
        key=lambda resource: resource.id,
    )


def _placement(
    index: GraphIndex, entry: Entry, collector: PlacementCollector
) -> SourceInterface | None:
    if isinstance(entry, NetworkInterface):
        return interface_placement(index, entry, collector)
    found = instance_placement(index, entry, collector)
    return found[0] if found else None


def _groups(index: GraphIndex, interface: SourceInterface) -> list[SecurityGroup]:
    found = (index.get_resource(group_id) for group_id in interface.security_group_ids)
    return [group for group in found if isinstance(group, SecurityGroup)]


def _acl_entries(index: GraphIndex, subnet_id: str) -> list[NaclEntry]:
    subnet = index.get_resource(subnet_id)
    association = sole_associate(
        index, subnet, AttachmentKind.NETWORK_ACL_ASSOCIATION, NetworkAcl, "network_acl"
    )
    return [] if isinstance(association, NetworkResult) else association[0].entries


def _needs(message: str) -> NetworkResult:
    return missing(message, element="internet_exposure")


def _piece(
    index: GraphIndex,
    address: NetworkResult,
    interface: SourceInterface,
    groups: list[SecurityGroup],
    piece: PortRange,
) -> dict[ExposureElement, NetworkResult]:
    route = evaluate_subnet_route(
        index, interface.subnet_id, PROBE_ADDRESS, reach=RouteReach.INTERNET_GATEWAY
    )
    return {
        _E.PUBLIC_ADDRESS: address,
        _E.ROUTE: route,
        _E.NACL: nacl_round_trip(
            index,
            interface.subnet_id,
            _INGRESS,
            INTERNET_CIDR,
            protocol=piece.protocol,
            port=piece.start,
        ),
        _E.SECURITY_GROUP: groups_allow(
            index,
            (group.id for group in groups),
            _INGRESS,
            Peer.within(INTERNET_CIDR, ()),
            protocol=piece.protocol,
            port=piece.start,
        ),
    }


def _without_ports(
    index: GraphIndex,
    entry: Entry,
    address: NetworkResult,
    groups: list[SecurityGroup],
    notes: tuple[AnalysisLimitation, ...],
    uncertain: bool,
    evidence: set[str],
) -> ExposureEvaluation:
    """No range is open to the internet: the groups deny it, unless some rule is unsupported."""
    denial = any_of(
        unsupported(
            "A security-group rule that might admit the internet is not supported.",
            element="security_group_rule",
            resource_id=group.id,
        )
        if uncertain
        else implicit_deny(index, group, "No security-group rule admits the internet on any port.")
        for group in groups
    )
    return evaluate_elements(
        ExposureEntryKind.PUBLIC_ADDRESS,
        entry,
        ExposureProtocol.TCP,
        _WHOLE_RANGE,
        {_E.PUBLIC_ADDRESS: address, _E.SECURITY_GROUP: denial},
        evidence=evidence,
        limitations=notes,
    )


def _unplaced(
    entry: Entry, address: NetworkResult, collector: PlacementCollector
) -> ExposureEvaluation:
    """Without a subnet and security groups nothing but the address can be assessed."""
    unplaced = _needs("The entry's network placement is not established.")
    return evaluate_elements(
        ExposureEntryKind.PUBLIC_ADDRESS,
        entry,
        ExposureProtocol.TCP,
        _WHOLE_RANGE,
        {
            _E.PUBLIC_ADDRESS: address,
            _E.ROUTE: unplaced,
            _E.NACL: unplaced,
            _E.SECURITY_GROUP: unplaced,
        },
        evidence=collector.evidence,
        limitations=collector.sorted_limitations(ignore=_UNNEEDED),
    )


def evaluate_public_entry(index: GraphIndex, entry: Entry) -> list[ExposureEvaluation]:
    """The candidate exposure of the entry, one evaluation per uniformly decided port range."""
    collector = PlacementCollector(index)
    address = evaluate_public_address(index, entry.id)
    interface = _placement(index, entry, collector)
    if interface is None:
        return [_unplaced(entry, address, collector)]
    groups = _groups(index, interface)
    opened = internet_open_ports(groups)
    notes = (*collector.sorted_limitations(ignore=_UNNEEDED), *opened.limitations)
    evidence = {*collector.evidence, *opened.evidence_ids}
    if not opened.ranges:
        return [_without_ports(index, entry, address, groups, notes, opened.uncertain, evidence)]
    acl_entries = _acl_entries(index, interface.subnet_id)
    evaluations = [
        evaluate_elements(
            ExposureEntryKind.PUBLIC_ADDRESS,
            entry,
            piece.protocol,
            (piece.start, piece.end),
            _piece(index, address, interface, groups, piece),
            evidence=evidence,
            limitations=notes,
        )
        for opened_range in opened.ranges
        for piece in split(opened_range, boundaries(groups, acl_entries, opened_range.protocol))
    ]
    return merge_adjacent(evaluations)

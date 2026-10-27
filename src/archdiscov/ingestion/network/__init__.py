"""Region-level normalization of EC2 networking resources."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ...models.architecture import Dependency, Evidence, RegionArchitecture
from ...models.enums import AwsRegion, ResourceType
from ..ec2 import Ec2Normalizer
from ..operations import Ec2Operation
from .containment import place
from .relationships import build_dependencies
from .resources import NetworkNormalizer, Normalized
from .transit import (
    TransitGatewayTableReads,
    transit_gateway,
    transit_gateway_attachment,
    transit_gateway_route_table,
)

if TYPE_CHECKING:
    # Only annotated: detail_execution loads botocore, which analysis must not import.
    from ..detail_execution import DetailResponse

Op = Ec2Operation


@dataclass
class RegionResult:
    architecture: RegionArchitecture
    evidence: dict[str, Evidence]
    dependencies: list[Dependency] = field(default_factory=list)


def _collect(
    items: Mapping[Ec2Operation, list[dict[str, Any]]], operation: Ec2Operation, key: str
) -> list[tuple[int, dict[str, Any]]]:
    """Each item of the operation that names its identifier, with its position."""
    return [(i, x) for i, x in enumerate(items.get(operation, [])) if x.get(key)]


def _ec2_entries(
    ec2: Ec2Normalizer, items: Mapping[Ec2Operation, list[dict[str, Any]]]
) -> list[Normalized]:
    entries = [
        Normalized(ec2.vpc(i, item), item, f"Vpcs[{i}]", Op.DESCRIBE_VPCS)
        for i, item in _collect(items, Op.DESCRIBE_VPCS, "VpcId")
    ]
    entries.extend(
        Normalized(
            ec2.subnet(i, item),
            item,
            f"Subnets[{i}]",
            Op.DESCRIBE_SUBNETS,
            vpc_id=item.get("VpcId"),
        )
        for i, item in _collect(items, Op.DESCRIBE_SUBNETS, "SubnetId")
    )
    for r, reservation in enumerate(items.get(Op.DESCRIBE_INSTANCES, [])):
        for i, item in enumerate(reservation.get("Instances") or []):
            if not item.get("InstanceId"):
                continue
            path = f"Reservations[{r}].Instances[{i}]"
            entries.append(
                Normalized(
                    ec2.instance(path, item),
                    item,
                    path,
                    Op.DESCRIBE_INSTANCES,
                    vpc_id=item.get("VpcId"),
                    subnet_id=item.get("SubnetId"),
                )
            )
    return entries


def _rules_by_group(
    items: Mapping[Ec2Operation, list[dict[str, Any]]], unavailable: Mapping[Ec2Operation, str]
) -> dict[str, list[tuple[int, dict[str, Any]]]] | None:
    """Each group's rules from the rule API; None when that read did not complete."""
    if Op.DESCRIBE_SECURITY_GROUP_RULES in unavailable:
        return None
    rules: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for i, rule in _collect(items, Op.DESCRIBE_SECURITY_GROUP_RULES, "GroupId"):
        rules.setdefault(rule["GroupId"], []).append((i, rule))
    return rules


def _shared_transit_gateways(
    items: Mapping[Ec2Operation, list[dict[str, Any]]], account_id: str
) -> dict[tuple[ResourceType, str], str]:
    """Transit gateways shared from another account, with their owner.

    They belong to that account, so they are not stored here and references to them are
    external.
    """
    return {
        (ResourceType.TRANSIT_GATEWAY, item["TransitGatewayId"]): item["OwnerId"]
        for _, item in _collect(items, Op.DESCRIBE_TRANSIT_GATEWAYS, "TransitGatewayId")
        if item.get("OwnerId") not in (None, account_id)
    }


def normalize_region(
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
    items: Mapping[Ec2Operation, list[dict[str, Any]]],
    unavailable: Mapping[Ec2Operation, str] | None = None,
    prefix_list_entries: Mapping[str, DetailResponse] | None = None,
    transit_gateway_tables: Mapping[str, TransitGatewayTableReads] | None = None,
) -> RegionResult:
    """Normalize one region. `unavailable` maps operations that did not fully complete to why;
    `prefix_list_entries` holds the entries response of each list read in full, and
    `transit_gateway_tables` each transit gateway route table's detail reads, by ID."""
    unavailable = unavailable or {}
    prefix_list_entries = prefix_list_entries or {}
    transit_gateway_tables = transit_gateway_tables or {}
    ec2 = Ec2Normalizer(snapshot_id, account_id, region)
    net = NetworkNormalizer(snapshot_id, account_id, region)
    rules = _rules_by_group(items, unavailable)
    shared = _shared_transit_gateways(items, account_id)
    normalizers: tuple[tuple[Ec2Operation, str, Callable[[int, dict[str, Any]], Normalized]], ...]
    normalizers = (
        (Op.DESCRIBE_ROUTE_TABLES, "RouteTableId", net.route_table),
        (Op.DESCRIBE_INTERNET_GATEWAYS, "InternetGatewayId", net.internet_gateway),
        (Op.DESCRIBE_NETWORK_ACLS, "NetworkAclId", net.network_acl),
        (
            Op.DESCRIBE_SECURITY_GROUPS,
            "GroupId",
            lambda i, item: net.security_group(
                i, item, None if rules is None else rules.get(item["GroupId"], [])
            ),
        ),
        (Op.DESCRIBE_NETWORK_INTERFACES, "NetworkInterfaceId", net.network_interface),
        (Op.DESCRIBE_NAT_GATEWAYS, "NatGatewayId", net.nat_gateway),
        (
            Op.DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS,
            "EgressOnlyInternetGatewayId",
            net.egress_only_internet_gateway,
        ),
        (Op.DESCRIBE_VPC_PEERING_CONNECTIONS, "VpcPeeringConnectionId", net.vpc_peering_connection),
        (Op.DESCRIBE_VPC_ENDPOINTS, "VpcEndpointId", net.vpc_endpoint),
        (
            Op.DESCRIBE_MANAGED_PREFIX_LISTS,
            "PrefixListId",
            lambda i, item: net.managed_prefix_list(
                i, item, prefix_list_entries.get(item["PrefixListId"])
            ),
        ),
        (Op.DESCRIBE_TRANSIT_GATEWAYS, "TransitGatewayId", partial(transit_gateway, net)),
        (
            Op.DESCRIBE_TRANSIT_GATEWAY_ATTACHMENTS,
            "TransitGatewayAttachmentId",
            partial(transit_gateway_attachment, net),
        ),
        (
            Op.DESCRIBE_TRANSIT_GATEWAY_ROUTE_TABLES,
            "TransitGatewayRouteTableId",
            lambda i, item: transit_gateway_route_table(
                net, i, item, transit_gateway_tables.get(item["TransitGatewayRouteTableId"])
            ),
        ),
    )
    entries = _ec2_entries(ec2, items)
    for operation, key, normalize in normalizers:
        entries.extend(
            normalize(i, item)
            for i, item in _collect(items, operation, key)
            if operation is not Op.DESCRIBE_TRANSIT_GATEWAYS
            or (ResourceType.TRANSIT_GATEWAY, item[key]) not in shared
        )

    roots = place(entries)
    dependencies = build_dependencies(net, entries, unavailable, shared)
    return RegionResult(
        RegionArchitecture(resources=roots),  # type: ignore[arg-type]
        {**ec2.evidence, **net.evidence},
        dependencies,
    )

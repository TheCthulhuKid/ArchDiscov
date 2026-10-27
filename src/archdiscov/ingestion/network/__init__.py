"""Region-level normalization of EC2 networking resources."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ...models.architecture import Dependency, Evidence, RegionArchitecture
from ...models.enums import AwsRegion
from ..ec2 import Ec2Normalizer
from ..operations import Ec2Operation
from .containment import place
from .relationships import build_dependencies
from .resources import NetworkNormalizer, Normalized

Op = Ec2Operation


@dataclass
class RegionResult:
    architecture: RegionArchitecture
    evidence: dict[str, Evidence]
    dependencies: list[Dependency] = field(default_factory=list)


def _have(item: dict[str, Any], key: str) -> bool:
    return bool(item.get(key))


def normalize_region(
    snapshot_id: UUID,
    account_id: str,
    region: AwsRegion,
    items: Mapping[Ec2Operation, list[dict[str, Any]]],
    unavailable: Mapping[Ec2Operation, str] | None = None,
) -> RegionResult:
    """Normalize one region. `unavailable` maps operations that did not fully complete to why."""
    unavailable = unavailable or {}
    ec2 = Ec2Normalizer(snapshot_id, account_id, region)
    net = NetworkNormalizer(snapshot_id, account_id, region)
    entries: list[Normalized] = []

    def collect(operation: Ec2Operation, key: str) -> list[tuple[int, dict[str, Any]]]:
        return [(i, x) for i, x in enumerate(items.get(operation, [])) if _have(x, key)]

    for i, item in collect(Op.DESCRIBE_VPCS, "VpcId"):
        entries.append(Normalized(ec2.vpc(i, item), item, f"Vpcs[{i}]", Op.DESCRIBE_VPCS))
    for i, item in collect(Op.DESCRIBE_SUBNETS, "SubnetId"):
        entries.append(
            Normalized(
                ec2.subnet(i, item),
                item,
                f"Subnets[{i}]",
                Op.DESCRIBE_SUBNETS,
                vpc_id=item.get("VpcId"),
            )
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
    for i, item in collect(Op.DESCRIBE_ROUTE_TABLES, "RouteTableId"):
        entries.append(net.route_table(i, item))
    for i, item in collect(Op.DESCRIBE_INTERNET_GATEWAYS, "InternetGatewayId"):
        entries.append(net.internet_gateway(i, item))
    for i, item in collect(Op.DESCRIBE_NETWORK_ACLS, "NetworkAclId"):
        entries.append(net.network_acl(i, item))
    rules_available = Op.DESCRIBE_SECURITY_GROUP_RULES not in unavailable
    rules_by_group: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for i, rule in collect(Op.DESCRIBE_SECURITY_GROUP_RULES, "GroupId"):
        rules_by_group.setdefault(rule["GroupId"], []).append((i, rule))
    for i, item in collect(Op.DESCRIBE_SECURITY_GROUPS, "GroupId"):
        rules = rules_by_group.get(item["GroupId"], []) if rules_available else None
        entries.append(net.security_group(i, item, rules))
    for i, item in collect(Op.DESCRIBE_NETWORK_INTERFACES, "NetworkInterfaceId"):
        entries.append(net.network_interface(i, item))
    for i, item in collect(Op.DESCRIBE_NAT_GATEWAYS, "NatGatewayId"):
        entries.append(net.nat_gateway(i, item))
    for i, item in collect(
        Op.DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS, "EgressOnlyInternetGatewayId"
    ):
        entries.append(net.egress_only_internet_gateway(i, item))

    roots = place(entries)
    dependencies = build_dependencies(net, entries, unavailable)
    return RegionResult(
        RegionArchitecture(resources=roots),  # type: ignore[arg-type]
        {**ec2.evidence, **net.evidence},
        dependencies,
    )

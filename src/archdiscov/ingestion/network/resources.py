"""Normalize route tables, gateways, NACLs, security groups, and network interfaces."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from ...models.enums import NaclAction, RouteTargetKind, RuleDirection
from ...models.resource_base import Resource
from ...models.resources.network import (
    InternetGateway,
    NaclEntry,
    NetworkAcl,
    NetworkInterface,
    PrivateIpAddress,
    Route,
    RouteTable,
    SecurityGroup,
    SecurityGroupRule,
)
from ..evidence_builder import EvidenceBuilder, tags_of
from ..operations import Ec2Operation as Op

FALLBACK_LIMITATION = (
    "Rule IDs are unavailable: DescribeSecurityGroupRules did not complete, "
    "so rules were derived from DescribeSecurityGroups permissions."
)


@dataclass
class Normalized:
    """A stored resource with the raw item and path it came from, for placement and links."""

    resource: Resource
    item: dict[str, Any]
    path: str
    operation: Op
    vpc_id: str | None = None
    subnet_id: str | None = None


_GATEWAY_PREFIXES = (
    ("igw-", RouteTargetKind.INTERNET_GATEWAY),
    ("vpce-", RouteTargetKind.VPC_ENDPOINT),
)
_ROUTE_TARGET_FIELDS = (
    ("NatGatewayId", RouteTargetKind.NAT_GATEWAY),
    ("TransitGatewayId", RouteTargetKind.TRANSIT_GATEWAY),
    ("VpcPeeringConnectionId", RouteTargetKind.VPC_PEERING),
    ("NetworkInterfaceId", RouteTargetKind.NETWORK_INTERFACE),
    ("InstanceId", RouteTargetKind.INSTANCE),
    ("VpcEndpointId", RouteTargetKind.VPC_ENDPOINT),
)
_OTHER_TARGET_FIELDS = (
    "EgressOnlyInternetGatewayId",
    "CarrierGatewayId",
    "LocalGatewayId",
    "CoreNetworkArn",
)


def classify_route_target(route: dict[str, Any]) -> tuple[RouteTargetKind, str | None, str | None]:
    """Return (kind, target id, source field) for a route; unknown when unparseable."""
    gateway = route.get("GatewayId")
    if gateway == "local":
        return RouteTargetKind.LOCAL, None, "GatewayId"
    if isinstance(gateway, str) and gateway:
        for prefix, kind in _GATEWAY_PREFIXES:
            if gateway.startswith(prefix):
                return kind, gateway, "GatewayId"
        return RouteTargetKind.UNKNOWN, gateway, "GatewayId"
    for name, kind in _ROUTE_TARGET_FIELDS:
        if route.get(name):
            return kind, route[name], name
    for name in _OTHER_TARGET_FIELDS:
        if route.get(name):
            return RouteTargetKind.UNKNOWN, route[name], name
    return RouteTargetKind.UNKNOWN, None, None


def _destination(route: dict[str, Any]) -> str | None:
    return (
        route.get("DestinationCidrBlock")
        or route.get("DestinationIpv6CidrBlock")
        or route.get("DestinationPrefixListId")
    )


def _validated[M: BaseModel](
    model: type[M], path: str, limitations: list[str], **data: Any
) -> M | None:
    """Build a typed value; malformed fields are dropped and recorded, never fatal."""
    clean = {k: v for k, v in data.items() if v is not None}
    for _ in range(2):
        try:
            return model(**clean)
        except ValidationError as error:
            bad = {str(e["loc"][0]) for e in error.errors() if e["loc"]}
            if not bad & clean.keys():
                break
            limitations.append(f"{path}: invalid value for {', '.join(sorted(bad))} was omitted")
            clean = {k: v for k, v in clean.items() if k not in bad}
    limitations.append(f"{path}: entry could not be normalized and was omitted")
    return None


def _protocol(value: Any) -> str | None:
    return None if value is None else str(value)


class NetworkNormalizer(EvidenceBuilder):
    """Builds network resources; free-text descriptions are redacted from evidence."""

    redact_keys = frozenset({"Description"})

    def _with_limitations(self, resource: Resource, limitations: list[str]) -> None:
        if limitations:
            resource.attributes["limitations"] = limitations

    def route_table(self, index: int, item: dict[str, Any]) -> Normalized:
        path, limitations = f"RouteTables[{index}]", []
        tags = tags_of(item)
        routes: list[Route] = []
        cited: list[tuple[int, dict[str, Any]]] = []
        for i, raw in enumerate(item.get("Routes") or []):
            kind, target_id, _ = classify_route_target(raw)
            route = _validated(
                Route,
                f"{path}.Routes[{i}]",
                limitations,
                destination=_destination(raw),
                target_kind=kind,
                target_id=target_id,
                state=raw.get("State"),
            )
            if route is not None:
                routes.append(route)
                cited.append((i, raw))
        associations = item.get("Associations") or []
        facts = {"/attributes/tags": ("Tags", item.get("Tags"))} if tags else {}
        if item.get("VpcId"):
            facts["/attributes/vpc_id"] = ("VpcId", item["VpcId"])
        resource = self._build(
            RouteTable,
            operation=Op.DESCRIBE_ROUTE_TABLES,
            item_path=path,
            item=item,
            physical_id=item["RouteTableId"],
            name=tags.get("Name"),
            attributes={
                "vpc_id": item.get("VpcId"),
                "is_main": any(a.get("Main") for a in associations) or None,
                "tags": tags or None,
            },
            facts=facts,
            routes=routes,
        )
        for n, (i, raw) in enumerate(cited):
            self.cite(
                resource,
                f"/routes/{n}",
                Op.DESCRIBE_ROUTE_TABLES,
                f"{path}.Routes[{i}]",
                raw,
            )
        self._with_limitations(resource, limitations)
        return Normalized(resource, item, path, Op.DESCRIBE_ROUTE_TABLES, vpc_id=item.get("VpcId"))

    def internet_gateway(self, index: int, item: dict[str, Any]) -> Normalized:
        path = f"InternetGateways[{index}]"
        tags = tags_of(item)
        attached = [a["VpcId"] for a in item.get("Attachments") or [] if a.get("VpcId")]
        facts: dict[str, tuple[str, Any]] = {}
        if attached:
            facts["/attributes/attached_vpc_ids"] = ("Attachments", item["Attachments"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        resource = self._build(
            InternetGateway,
            operation=Op.DESCRIBE_INTERNET_GATEWAYS,
            item_path=path,
            item=item,
            physical_id=item["InternetGatewayId"],
            name=tags.get("Name"),
            attributes={"attached_vpc_ids": attached or None, "tags": tags or None},
            facts=facts,
        )
        return Normalized(
            resource,
            item,
            path,
            Op.DESCRIBE_INTERNET_GATEWAYS,
            vpc_id=attached[0] if attached else None,
        )

    def network_acl(self, index: int, item: dict[str, Any]) -> Normalized:
        path, limitations = f"NetworkAcls[{index}]", []
        tags = tags_of(item)
        entries: list[NaclEntry] = []
        cited: list[tuple[int, dict[str, Any]]] = []
        for i, raw in enumerate(item.get("Entries") or []):
            ports = raw.get("PortRange") or {}
            try:
                action, direction = (
                    NaclAction(str(raw.get("RuleAction"))),
                    RuleDirection.EGRESS if raw.get("Egress") else RuleDirection.INGRESS,
                )
            except ValueError:
                limitations.append(f"{path}.Entries[{i}]: unknown rule action was omitted")
                continue
            entry = _validated(
                NaclEntry,
                f"{path}.Entries[{i}]",
                limitations,
                rule_number=raw.get("RuleNumber"),
                direction=direction,
                action=action,
                protocol=_protocol(raw.get("Protocol")),
                from_port=ports.get("From"),
                to_port=ports.get("To"),
                cidr=raw.get("CidrBlock") or raw.get("Ipv6CidrBlock"),
            )
            if entry is not None:
                entries.append(entry)
                cited.append((i, raw))
        facts: dict[str, tuple[str, Any]] = {}
        if item.get("VpcId"):
            facts["/attributes/vpc_id"] = ("VpcId", item["VpcId"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        resource = self._build(
            NetworkAcl,
            operation=Op.DESCRIBE_NETWORK_ACLS,
            item_path=path,
            item=item,
            physical_id=item["NetworkAclId"],
            name=tags.get("Name"),
            attributes={
                "vpc_id": item.get("VpcId"),
                "is_default": item.get("IsDefault"),
                "tags": tags or None,
            },
            facts=facts,
            entries=entries,
        )
        for n, (i, raw) in enumerate(cited):
            self.cite(
                resource,
                f"/entries/{n}",
                Op.DESCRIBE_NETWORK_ACLS,
                f"{path}.Entries[{i}]",
                raw,
            )
        self._with_limitations(resource, limitations)
        return Normalized(resource, item, path, Op.DESCRIBE_NETWORK_ACLS, vpc_id=item.get("VpcId"))

    def security_group(
        self,
        index: int,
        item: dict[str, Any],
        rules: list[tuple[int, dict[str, Any]]] | None,
    ) -> Normalized:
        """`rules` are this group's DescribeSecurityGroupRules items, or None when unavailable."""
        path, limitations = f"SecurityGroups[{index}]", []
        tags = tags_of(item)
        sources = (
            self._rules_from_rule_items(rules, limitations)
            if rules is not None
            else self._rules_from_permissions(path, item, limitations)
        )
        if rules is None:
            limitations.insert(0, FALLBACK_LIMITATION)
        built: dict[RuleDirection, list[tuple[SecurityGroupRule, str, str, Any]]] = {
            RuleDirection.INGRESS: [],
            RuleDirection.EGRESS: [],
        }
        for rule, operation, source_path, value in sources:
            built[rule.direction].append((rule, operation, source_path, value))
        facts: dict[str, tuple[str, Any]] = {}
        if item.get("VpcId"):
            facts["/attributes/vpc_id"] = ("VpcId", item["VpcId"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        resource = self._build(
            SecurityGroup,
            operation=Op.DESCRIBE_SECURITY_GROUPS,
            item_path=path,
            item=item,
            physical_id=item["GroupId"],
            name=tags.get("Name") or item.get("GroupName"),
            attributes={
                "group_name": item.get("GroupName"),
                "vpc_id": item.get("VpcId"),
                "owner_id": item.get("OwnerId"),
                "rules_source": "DescribeSecurityGroupRules"
                if rules is not None
                else "DescribeSecurityGroups",
                "tags": tags or None,
            },
            facts=facts,
            ingress_rules=[r for r, *_ in built[RuleDirection.INGRESS]],
            egress_rules=[r for r, *_ in built[RuleDirection.EGRESS]],
        )
        for direction, name in (
            (RuleDirection.INGRESS, "ingress_rules"),
            (RuleDirection.EGRESS, "egress_rules"),
        ):
            for n, (_, operation, source_path, value) in enumerate(built[direction]):
                self.cite(resource, f"/{name}/{n}", operation, source_path, value)
        self._with_limitations(resource, limitations)
        return Normalized(
            resource, item, path, Op.DESCRIBE_SECURITY_GROUPS, vpc_id=item.get("VpcId")
        )

    def _rules_from_rule_items(
        self, rules: list[tuple[int, dict[str, Any]]], limitations: list[str]
    ) -> list[tuple[SecurityGroupRule, str, str, Any]]:
        out = []
        for i, raw in rules:
            source_path = f"SecurityGroupRules[{i}]"
            referenced = raw.get("ReferencedGroupInfo") or {}
            rule = _validated(
                SecurityGroupRule,
                source_path,
                limitations,
                direction=RuleDirection.EGRESS if raw.get("IsEgress") else RuleDirection.INGRESS,
                rule_id=raw.get("SecurityGroupRuleId"),
                protocol=_protocol(raw.get("IpProtocol")),
                from_port=raw.get("FromPort"),
                to_port=raw.get("ToPort"),
                cidr=raw.get("CidrIpv4") or raw.get("CidrIpv6"),
                referenced_group_id=referenced.get("GroupId"),
                referenced_account_id=referenced.get("UserId"),
                prefix_list_id=raw.get("PrefixListId"),
            )
            if rule is not None:
                out.append((rule, Op.DESCRIBE_SECURITY_GROUP_RULES, source_path, raw))
        return out

    def _rules_from_permissions(
        self, group_path: str, item: dict[str, Any], limitations: list[str]
    ) -> list[tuple[SecurityGroupRule, str, str, Any]]:
        out = []
        for field, direction in (
            ("IpPermissions", RuleDirection.INGRESS),
            ("IpPermissionsEgress", RuleDirection.EGRESS),
        ):
            for j, permission in enumerate(item.get(field) or []):
                source_path = f"{group_path}.{field}[{j}]"
                peers: list[dict[str, Any]] = [
                    *({"cidr": r.get("CidrIp")} for r in permission.get("IpRanges") or []),
                    *({"cidr": r.get("CidrIpv6")} for r in permission.get("Ipv6Ranges") or []),
                    *(
                        {
                            "referenced_group_id": p.get("GroupId"),
                            "referenced_account_id": p.get("UserId"),
                        }
                        for p in permission.get("UserIdGroupPairs") or []
                    ),
                    *(
                        {"prefix_list_id": p.get("PrefixListId")}
                        for p in permission.get("PrefixListIds") or []
                    ),
                ] or [{}]
                for peer in peers:
                    rule = _validated(
                        SecurityGroupRule,
                        source_path,
                        limitations,
                        direction=direction,
                        protocol=_protocol(permission.get("IpProtocol")),
                        from_port=permission.get("FromPort"),
                        to_port=permission.get("ToPort"),
                        **peer,
                    )
                    if rule is not None:
                        out.append((rule, Op.DESCRIBE_SECURITY_GROUPS, source_path, permission))
        return out

    def network_interface(self, index: int, item: dict[str, Any]) -> Normalized:
        path, limitations = f"NetworkInterfaces[{index}]", []
        tags = tags_of(item)
        addresses = []
        for i, raw in enumerate(item.get("PrivateIpAddresses") or []):
            address = _validated(
                PrivateIpAddress,
                f"{path}.PrivateIpAddresses[{i}]",
                limitations,
                address=raw.get("PrivateIpAddress"),
                primary=bool(raw.get("Primary")),
                public_ip=(raw.get("Association") or {}).get("PublicIp"),
            )
            if address is not None:
                addresses.append(address)
        public_ip = (item.get("Association") or {}).get("PublicIp")
        groups = [g["GroupId"] for g in item.get("Groups") or [] if g.get("GroupId")]
        instance_id = (item.get("Attachment") or {}).get("InstanceId")
        facts: dict[str, tuple[str, Any]] = {}
        if addresses:
            facts["/private_ip_addresses"] = ("PrivateIpAddresses", item["PrivateIpAddresses"])
        if public_ip:
            facts["/public_ip"] = ("Association.PublicIp", public_ip)
        if item.get("MacAddress"):
            facts["/mac_address"] = ("MacAddress", item["MacAddress"])
        if groups:
            facts["/attributes/security_group_ids"] = ("Groups", item["Groups"])
        if instance_id:
            facts["/attributes/attached_instance_id"] = ("Attachment.InstanceId", instance_id)
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        resource = self._build(
            NetworkInterface,
            operation=Op.DESCRIBE_NETWORK_INTERFACES,
            item_path=path,
            item=item,
            physical_id=item["NetworkInterfaceId"],
            name=tags.get("Name"),
            attributes={
                "status": item.get("Status"),
                "interface_type": item.get("InterfaceType"),
                "vpc_id": item.get("VpcId"),
                "subnet_id": item.get("SubnetId"),
                "security_group_ids": groups or None,
                "attached_instance_id": instance_id,
                "tags": tags or None,
            },
            facts=facts,
            private_ip_addresses=addresses,
            public_ip=public_ip,
            mac_address=item.get("MacAddress"),
        )
        self._with_limitations(resource, limitations)
        return Normalized(
            resource,
            item,
            path,
            Op.DESCRIBE_NETWORK_INTERFACES,
            vpc_id=item.get("VpcId"),
            subnet_id=item.get("SubnetId"),
        )

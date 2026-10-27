"""Normalize route tables, gateways, NACLs, security groups, and network interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import AddressValueError, IPv4Address, IPv6Address
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ...models.enums import (
    AddressFamily,
    NaclAction,
    NatConnectivity,
    RouteTargetKind,
    RuleDirection,
    VpcEndpointType,
)
from ...models.resource_base import Resource
from ...models.resources.network import (
    EgressOnlyInternetGateway,
    InternetGateway,
    ManagedPrefixList,
    NaclEntry,
    NatGateway,
    NetworkAcl,
    NetworkInterface,
    PeeringSide,
    PrefixListEntry,
    PrivateIpAddress,
    Route,
    RouteTable,
    SecurityGroup,
    SecurityGroupRule,
    VpcEndpoint,
    VpcPeeringConnection,
)
from ..evidence_builder import EvidenceBuilder, tags_of
from ..operations import Ec2Operation as Op

if TYPE_CHECKING:
    # Only annotated: detail_execution loads botocore, which analysis must not import.
    from ..detail_execution import DetailResponse

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
    ("EgressOnlyInternetGatewayId", RouteTargetKind.EGRESS_ONLY_INTERNET_GATEWAY),
)
_ENDPOINT_TYPES = {
    "Gateway": VpcEndpointType.GATEWAY,
    "Interface": VpcEndpointType.INTERFACE,
    "GatewayLoadBalancer": VpcEndpointType.GATEWAY_LOAD_BALANCER,
}
_ADDRESS_FAMILIES = {"IPv4": AddressFamily.IPV4, "IPv6": AddressFamily.IPV6}
_OTHER_TARGET_FIELDS = (
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


def destination(route: dict[str, Any]) -> str | None:
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


def _ip_address[A: (IPv4Address, IPv6Address)](kind: type[A], value: Any) -> A | None:
    # The address classes also accept an integer, which the API never reports.
    if not isinstance(value, str):
        return None
    try:
        return kind(value)
    except AddressValueError:
        return None


def _peering_side(info: dict[str, Any]) -> PeeringSide:
    """One side of a peering connection; its CIDR sets when reported, else its primary CIDR."""
    ipv4 = [c["CidrBlock"] for c in info.get("CidrBlockSet") or [] if c.get("CidrBlock")]
    if not ipv4 and info.get("CidrBlock"):
        ipv4 = [info["CidrBlock"]]
    ipv6 = [
        c["Ipv6CidrBlock"] for c in info.get("Ipv6CidrBlockSet") or [] if c.get("Ipv6CidrBlock")
    ]
    return PeeringSide(
        vpc_id=info.get("VpcId"), owner_id=info.get("OwnerId"), cidr_blocks=[*ipv4, *ipv6]
    )


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
                destination=destination(raw),
                prefix_list_id=raw.get("DestinationPrefixListId"),
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

    def _ipv6_addresses(
        self, path: str, item: dict[str, Any], limitations: list[str]
    ) -> list[tuple[int, dict[str, Any], IPv6Address]] | None:
        """Each valid IPv6 address with its source entry; None when the field is absent."""
        if "Ipv6Addresses" not in item:
            return None
        out = []
        for i, raw in enumerate(item["Ipv6Addresses"] or []):
            address = _ip_address(IPv6Address, raw.get("Ipv6Address"))
            if address is None:
                limitations.append(
                    f"{path}.Ipv6Addresses[{i}]: invalid value for Ipv6Address was omitted"
                )
            else:
                out.append((i, raw, address))
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
        ipv6 = self._ipv6_addresses(path, item, limitations)
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
            ipv6_addresses=None if ipv6 is None else [address for _, _, address in ipv6],
        )
        for n, (i, raw, _) in enumerate(ipv6 or []):
            self.cite(
                resource,
                f"/ipv6_addresses/{n}",
                Op.DESCRIBE_NETWORK_INTERFACES,
                f"{path}.Ipv6Addresses[{i}]",
                raw,
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

    def nat_gateway(self, index: int, item: dict[str, Any]) -> Normalized:
        path, limitations = f"NatGateways[{index}]", []
        tags = tags_of(item)
        addresses = list(enumerate(item.get("NatGatewayAddresses") or []))
        public: list[tuple[int, IPv4Address]] = []
        for i, raw in addresses:
            if raw.get("PublicIp") is None:
                continue
            address = _ip_address(IPv4Address, raw["PublicIp"])
            if address is None:
                limitations.append(
                    f"{path}.NatGatewayAddresses[{i}]: invalid value for PublicIp was omitted"
                )
            else:
                public.append((i, address))
        interfaces = [(i, raw) for i, raw in addresses if raw.get("NetworkInterfaceId")]
        fallback = interfaces[0] if interfaces else None
        interface = next((x for x in interfaces if x[1].get("IsPrimary")), fallback)
        connectivity = self._nat_connectivity(path, item, limitations)
        facts = {
            pointer: (field, item[field])
            for pointer, field in (
                ("/state", "State"),
                ("/vpc_id", "VpcId"),
                ("/subnet_id", "SubnetId"),
            )
            if item.get(field)
        }
        if connectivity is not None:
            facts["/connectivity"] = ("ConnectivityType", item["ConnectivityType"])
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))

        def build(connectivity: NatConnectivity | None) -> Resource:
            return self._build(
                NatGateway,
                operation=Op.DESCRIBE_NAT_GATEWAYS,
                item_path=path,
                item=item,
                physical_id=item["NatGatewayId"],
                name=tags.get("Name"),
                attributes={"tags": tags or None},
                facts=facts,
                state=item.get("State") or None,
                connectivity=connectivity,
                vpc_id=item.get("VpcId") or None,
                subnet_id=item.get("SubnetId") or None,
                network_interface_id=interface[1]["NetworkInterfaceId"] if interface else None,
                public_ips=[address for _, address in public],
            )

        try:
            resource = build(connectivity)
        except ValidationError as error:
            # A model-level error has no field location; only the public-address rules raise one.
            if any(detail["loc"] for detail in error.errors()):
                raise
            limitations.append(f"{path}: {error.errors()[0]['msg']}; ConnectivityType was omitted")
            facts.pop("/connectivity", None)
            resource = build(None)
        address_path = f"{path}.NatGatewayAddresses"
        if interface is not None:
            i, raw = interface
            field = f"{address_path}[{i}].NetworkInterfaceId"
            self.cite(
                resource,
                "/network_interface_id",
                Op.DESCRIBE_NAT_GATEWAYS,
                field,
                raw["NetworkInterfaceId"],
            )
        for n, (i, address) in enumerate(public):
            field = f"{address_path}[{i}].PublicIp"
            self.cite(resource, f"/public_ips/{n}", Op.DESCRIBE_NAT_GATEWAYS, field, str(address))
        self._with_limitations(resource, limitations)
        return Normalized(
            resource,
            item,
            path,
            Op.DESCRIBE_NAT_GATEWAYS,
            vpc_id=item.get("VpcId"),
            subnet_id=item.get("SubnetId"),
        )

    @staticmethod
    def _nat_connectivity(
        path: str, item: dict[str, Any], limitations: list[str]
    ) -> NatConnectivity | None:
        value = item.get("ConnectivityType")
        if value is None:
            return None
        try:
            return NatConnectivity(value)
        except ValueError:
            limitations.append(f"{path}: invalid value for ConnectivityType was omitted")
            return None

    def egress_only_internet_gateway(self, index: int, item: dict[str, Any]) -> Normalized:
        path = f"EgressOnlyInternetGateways[{index}]"
        op = Op.DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS
        tags = tags_of(item)
        attachments = enumerate(item.get("Attachments") or [])
        attached = next(((i, a) for i, a in attachments if a.get("VpcId")), None)
        vpc_id = attached[1]["VpcId"] if attached else None
        resource = self._build(
            EgressOnlyInternetGateway,
            operation=op,
            item_path=path,
            item=item,
            physical_id=item["EgressOnlyInternetGatewayId"],
            name=tags.get("Name"),
            attributes={"tags": tags or None},
            facts={"/attributes/tags": ("Tags", item.get("Tags"))} if tags else {},
            vpc_id=vpc_id,
        )
        if attached is not None:
            i, attachment = attached
            self.cite(resource, "/vpc_id", op, f"{path}.Attachments[{i}]", attachment)
        return Normalized(resource, item, path, op, vpc_id=vpc_id)

    def vpc_peering_connection(self, index: int, item: dict[str, Any]) -> Normalized:
        """A regional root: the connection belongs to neither VPC alone."""
        path = f"VpcPeeringConnections[{index}]"
        op = Op.DESCRIBE_VPC_PEERING_CONNECTIONS
        tags = tags_of(item)
        status = item.get("Status") or {}
        requester = item.get("RequesterVpcInfo") or {}
        accepter = item.get("AccepterVpcInfo") or {}
        facts = {
            "/requester": ("RequesterVpcInfo", requester),
            "/accepter": ("AccepterVpcInfo", accepter),
        }
        if status.get("Code"):
            facts["/state"] = ("Status", status)
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        resource = self._build(
            VpcPeeringConnection,
            operation=op,
            item_path=path,
            item=item,
            physical_id=item["VpcPeeringConnectionId"],
            name=tags.get("Name"),
            attributes={"tags": tags or None},
            facts=facts,
            state=status.get("Code") or None,
            requester=_peering_side(requester),
            accepter=_peering_side(accepter),
        )
        return Normalized(resource, item, path, op)

    def vpc_endpoint(self, index: int, item: dict[str, Any]) -> Normalized:
        path, limitations = f"VpcEndpoints[{index}]", []
        op = Op.DESCRIBE_VPC_ENDPOINTS
        tags = tags_of(item)
        reported = item.get("VpcEndpointType")
        endpoint_type = _ENDPOINT_TYPES.get(reported) if isinstance(reported, str) else None
        if reported is not None and endpoint_type is None:
            limitations.append(f"{path}: VpcEndpointType is not a modelled type and was omitted")
        facts = {
            pointer: (field, item[field])
            for pointer, field in (
                ("/service_name", "ServiceName"),
                ("/state", "State"),
                ("/vpc_id", "VpcId"),
                ("/owner_id", "OwnerId"),
            )
            if item.get(field)
        }
        if endpoint_type is not None:
            facts["/endpoint_type"] = ("VpcEndpointType", reported)
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        # Normalized field -> (source field, [(source position, ID)]).
        ids = {
            name: (field, [(i, value) for i, value in enumerate(item.get(field) or []) if value])
            for name, field in (
                ("route_table_ids", "RouteTableIds"),
                ("subnet_ids", "SubnetIds"),
                ("network_interface_ids", "NetworkInterfaceIds"),
            )
        }
        groups = [
            (i, group) for i, group in enumerate(item.get("Groups") or []) if group.get("GroupId")
        ]
        resource = self._build(
            VpcEndpoint,
            operation=op,
            item_path=path,
            item=item,
            physical_id=item["VpcEndpointId"],
            name=tags.get("Name"),
            attributes={"tags": tags or None},
            facts=facts,
            endpoint_type=endpoint_type,
            service_name=item.get("ServiceName") or None,
            state=item.get("State") or None,
            vpc_id=item.get("VpcId") or None,
            owner_id=item.get("OwnerId") or None,
            security_group_ids=[group["GroupId"] for _, group in groups],
            **{name: [value for _, value in values] for name, (_, values) in ids.items()},
        )
        for name, (field, values) in ids.items():
            for n, (i, value) in enumerate(values):
                self.cite(resource, f"/{name}/{n}", op, f"{path}.{field}[{i}]", value)
        for n, (i, group) in enumerate(groups):
            self.cite(resource, f"/security_group_ids/{n}", op, f"{path}.Groups[{i}]", group)
        self._with_limitations(resource, limitations)
        return Normalized(resource, item, path, op, vpc_id=item.get("VpcId"))

    def managed_prefix_list(
        self, index: int, item: dict[str, Any], response: DetailResponse | None
    ) -> Normalized:
        """A regional root; `entries` stays unset unless the whole entries response is valid."""
        path, limitations = f"PrefixLists[{index}]", []
        op = Op.DESCRIBE_MANAGED_PREFIX_LISTS
        tags = tags_of(item)
        family = _ADDRESS_FAMILIES.get(item.get("AddressFamily"))
        version = item.get("Version")
        version = version if isinstance(version, int) and version >= 1 else None
        facts = {
            pointer: (field, item[field])
            for pointer, field in (("/owner_id", "OwnerId"), ("/state", "State"))
            if item.get(field)
        }
        if family is not None:
            facts["/address_family"] = ("AddressFamily", item["AddressFamily"])
        if version is not None:
            facts["/version"] = ("Version", version)
        if tags:
            facts["/attributes/tags"] = ("Tags", item.get("Tags"))
        entries = self._prefix_list_entries(path, response, limitations)
        resource = self._build(
            ManagedPrefixList,
            operation=op,
            item_path=path,
            item=item,
            physical_id=item["PrefixListId"],
            name=item.get("PrefixListName") or tags.get("Name"),
            attributes={"tags": tags or None},
            facts=facts,
            address_family=family,
            owner_id=item.get("OwnerId") or None,
            state=item.get("State") or None,
            version=version,
            entries=None if entries is None else [entry for _, _, entry in entries],
        )
        if response is not None:
            for n, (i, raw, _) in enumerate(entries or []):
                self.cite(
                    resource,
                    f"/entries/{n}",
                    Op.GET_MANAGED_PREFIX_LIST_ENTRIES,
                    f"{response.path}.Entries[{i}]",
                    raw,
                )
        self._with_limitations(resource, limitations)
        return Normalized(resource, item, path, op)

    @staticmethod
    def _prefix_list_entries(
        path: str, response: DetailResponse | None, limitations: list[str]
    ) -> list[tuple[int, dict[str, Any], PrefixListEntry]] | None:
        """Every entry, or None when unread or when any entry is invalid: never a partial list."""
        if response is None:
            return None
        raw_entries = response.value.get("Entries")
        if not isinstance(raw_entries, list):
            limitations.append(f"{path}: the entries response holds no list, so none were recorded")
            return None
        out = []
        for i, raw in enumerate(raw_entries):
            where = f"{response.path}.Entries[{i}]"
            cidr = raw.get("Cidr") if isinstance(raw, dict) else None
            entry = _validated(PrefixListEntry, where, limitations, cidr=cidr)
            if entry is None:
                limitations.append(f"{path}: an entry is invalid, so no entries were recorded")
                return None
            out.append((i, raw, entry))
        return out

"""Typed network resources.

Links between stored resources are recorded as dependencies. A field keeps an identifier only
when a decision compares it, such as a NAT gateway's subnet or a peering side's owner account.
"""

from ipaddress import IPv4Address, IPv6Address
from typing import Literal, Self

from pydantic import Field, IPvAnyNetwork, model_validator

from ..enums import (
    AddressFamily,
    NaclAction,
    NatConnectivity,
    ResourceType,
    RouteTargetKind,
    RuleDirection,
    TransitGatewayRouteType,
    VpcEndpointType,
)
from ..resource_base import Model, NonEmptyString, OmitsAbsentFields, Resource

Port = Field(default=None, ge=-1, le=65535)  # -1 means all ports or ICMP types
_ACCOUNT_ID = r"^[0-9]{12}$"
_IP_VERSIONS = {AddressFamily.IPV4: 4, AddressFamily.IPV6: 6}


class Route(OmitsAbsentFields):
    omitted_when_absent = frozenset({"prefix_list_id"})
    destination: str | None = None  # CIDR, IPv6 CIDR, or prefix list ID as reported
    prefix_list_id: NonEmptyString | None = None  # the destination, when it is a prefix list
    target_kind: RouteTargetKind
    target_id: str | None = None
    state: str | None = None


class SecurityGroupRule(Model):
    direction: RuleDirection
    rule_id: str | None = None
    protocol: str | None = None  # "-1" means all protocols
    from_port: int | None = Port
    to_port: int | None = Port
    cidr: IPvAnyNetwork | None = None
    referenced_group_id: str | None = None
    referenced_account_id: str | None = Field(default=None, pattern=_ACCOUNT_ID)
    prefix_list_id: str | None = None

    @model_validator(mode="after")
    def validate_single_source(self) -> Self:
        if self.cidr is not None and self.prefix_list_id is not None:
            raise ValueError("a rule names a CIDR or a prefix list, not both")
        return self


class NaclEntry(Model):
    rule_number: int
    direction: RuleDirection
    action: NaclAction
    protocol: str | None = None
    from_port: int | None = Port
    to_port: int | None = Port
    cidr: IPvAnyNetwork | None = None


class PrivateIpAddress(Model):
    address: NonEmptyString
    primary: bool = False
    public_ip: str | None = None


class RouteTable(Resource):
    resource_type: Literal[ResourceType.ROUTE_TABLE] = ResourceType.ROUTE_TABLE
    routes: list[Route] = Field(default_factory=list)


class InternetGateway(Resource):
    resource_type: Literal[ResourceType.INTERNET_GATEWAY] = ResourceType.INTERNET_GATEWAY


class NetworkAcl(Resource):
    resource_type: Literal[ResourceType.NETWORK_ACL] = ResourceType.NETWORK_ACL
    entries: list[NaclEntry] = Field(default_factory=list)


class SecurityGroup(Resource):
    resource_type: Literal[ResourceType.SECURITY_GROUP] = ResourceType.SECURITY_GROUP
    ingress_rules: list[SecurityGroupRule] = Field(default_factory=list)
    egress_rules: list[SecurityGroupRule] = Field(default_factory=list)


class NetworkInterface(Resource):
    omitted_when_absent = Resource.omitted_when_absent | {"ipv6_addresses"}
    resource_type: Literal[ResourceType.NETWORK_INTERFACE] = ResourceType.NETWORK_INTERFACE
    private_ip_addresses: list[PrivateIpAddress] = Field(default_factory=list)
    public_ip: str | None = None
    mac_address: str | None = None
    # None when not recorded. AWS assigns globally routable addresses, so whether one is
    # public follows from the address itself.
    ipv6_addresses: list[IPv6Address] | None = None


class NatGateway(Resource):
    resource_type: Literal[ResourceType.NAT_GATEWAY] = ResourceType.NAT_GATEWAY
    state: NonEmptyString | None = None  # as reported, for example "available"
    connectivity: NatConnectivity | None = None
    vpc_id: NonEmptyString | None = None
    subnet_id: NonEmptyString | None = None
    network_interface_id: NonEmptyString | None = None
    public_ips: list[IPv4Address] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_public_addresses(self) -> Self:
        if self.connectivity is NatConnectivity.PRIVATE and self.public_ips:
            raise ValueError("a private NAT gateway has no public address")
        if (
            self.connectivity is NatConnectivity.PUBLIC
            and self.state == "available"
            and not self.public_ips
        ):
            raise ValueError("an available public NAT gateway has a public address")
        return self


class EgressOnlyInternetGateway(Resource):
    resource_type: Literal[ResourceType.EGRESS_ONLY_INTERNET_GATEWAY] = (
        ResourceType.EGRESS_ONLY_INTERNET_GATEWAY
    )
    vpc_id: NonEmptyString | None = None


class PeeringSide(Model):
    # None when not recorded, such as a template's `!Ref` or a plan's unknown value.
    vpc_id: NonEmptyString | None = None
    owner_id: str | None = Field(default=None, pattern=_ACCOUNT_ID)
    cidr_blocks: list[IPvAnyNetwork] = Field(default_factory=list)


class VpcPeeringConnection(Resource):
    """Both sides as recorded; whether they share an account is a comparison of owner IDs."""

    resource_type: Literal[ResourceType.VPC_PEERING_CONNECTION] = (
        ResourceType.VPC_PEERING_CONNECTION
    )
    state: NonEmptyString | None = None  # as reported, for example "active"
    # A side whose fields are all None was not recorded, as in a template that refers to its VPC.
    requester: PeeringSide = Field(default_factory=PeeringSide)
    accepter: PeeringSide = Field(default_factory=PeeringSide)

    @model_validator(mode="after")
    def validate_distinct_sides(self) -> Self:
        recorded = self.requester.vpc_id is not None
        if recorded and self.requester.vpc_id == self.accepter.vpc_id:
            raise ValueError("a peering connection joins two different VPCs")
        return self


class TransitGateway(Resource):
    resource_type: Literal[ResourceType.TRANSIT_GATEWAY] = ResourceType.TRANSIT_GATEWAY
    state: NonEmptyString | None = None  # as reported, for example "available"
    owner_id: str | None = Field(default=None, pattern=_ACCOUNT_ID)
    association_default_route_table_id: NonEmptyString | None = None
    propagation_default_route_table_id: NonEmptyString | None = None


class TransitGatewayAttachment(Resource):
    resource_type: Literal[ResourceType.TRANSIT_GATEWAY_ATTACHMENT] = (
        ResourceType.TRANSIT_GATEWAY_ATTACHMENT
    )
    state: NonEmptyString | None = None
    transit_gateway_id: NonEmptyString | None = None
    # As reported, for example "vpc" or "peering"; AWS adds kinds over time.
    attached_resource_type: NonEmptyString | None = None
    attached_resource_id: NonEmptyString | None = None
    attached_resource_owner_id: str | None = Field(default=None, pattern=_ACCOUNT_ID)
    association_state: NonEmptyString | None = None
    associated_route_table_id: NonEmptyString | None = None


class TransitGatewayLink(Model):
    """One attachment's association with, or propagation to, a route table."""

    attachment_id: NonEmptyString
    state: NonEmptyString | None = None


class TransitGatewayRoute(Model):
    destination: str | None = None  # CIDR or prefix list ID as reported
    attachment_ids: list[NonEmptyString] = Field(default_factory=list)
    route_type: TransitGatewayRouteType | None = None
    state: NonEmptyString | None = None  # as reported, for example "active" or "blackhole"

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if (self.state == "blackhole") == bool(self.attachment_ids):
            raise ValueError("a transit gateway route names attachments unless it is a blackhole")
        return self


class TransitGatewayRouteTable(Resource):
    resource_type: Literal[ResourceType.TRANSIT_GATEWAY_ROUTE_TABLE] = (
        ResourceType.TRANSIT_GATEWAY_ROUTE_TABLE
    )
    state: NonEmptyString | None = None
    transit_gateway_id: NonEmptyString | None = None
    associations: list[TransitGatewayLink] = Field(default_factory=list)
    propagations: list[TransitGatewayLink] = Field(default_factory=list)
    routes: list[TransitGatewayRoute] = Field(default_factory=list)
    # False when the route search reported more routes than it returned; None when unrecorded.
    routes_complete: bool | None = None


class VpcEndpoint(Resource):
    resource_type: Literal[ResourceType.VPC_ENDPOINT] = ResourceType.VPC_ENDPOINT
    endpoint_type: VpcEndpointType | None = None
    service_name: NonEmptyString | None = None
    state: NonEmptyString | None = None  # as reported, for example "available"
    vpc_id: NonEmptyString | None = None
    owner_id: str | None = Field(default=None, pattern=_ACCOUNT_ID)
    route_table_ids: list[NonEmptyString] = Field(default_factory=list)
    subnet_ids: list[NonEmptyString] = Field(default_factory=list)
    network_interface_ids: list[NonEmptyString] = Field(default_factory=list)
    security_group_ids: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_placement(self) -> Self:
        if self.endpoint_type is VpcEndpointType.GATEWAY and self.subnet_ids:
            raise ValueError("a gateway endpoint has no subnets")
        if self.endpoint_type is VpcEndpointType.INTERFACE and self.route_table_ids:
            raise ValueError("an interface endpoint has no route tables")
        return self


class PrefixListEntry(Model):
    cidr: IPvAnyNetwork


class ManagedPrefixList(Resource):
    resource_type: Literal[ResourceType.MANAGED_PREFIX_LIST] = ResourceType.MANAGED_PREFIX_LIST
    address_family: AddressFamily | None = None
    # An account ID, or "AWS" for a list AWS manages, such as a service's gateway prefix list.
    owner_id: str | None = Field(default=None, pattern=r"^([0-9]{12}|AWS)$")
    state: NonEmptyString | None = None  # as reported, for example "create-complete"
    version: int | None = Field(default=None, ge=1)
    # None when the entries were not recorded; an empty list is a recorded empty list.
    entries: list[PrefixListEntry] | None = None

    @model_validator(mode="after")
    def validate_entry_family(self) -> Self:
        if self.address_family is None:
            return self
        for entry in self.entries or []:
            if entry.cidr.version != _IP_VERSIONS[self.address_family]:
                raise ValueError(f"entry {entry.cidr} is not {self.address_family.value}")
        return self

"""Typed network resources; relationships between them are dependencies, not fields."""

from typing import Literal

from pydantic import Field, IPvAnyNetwork

from .enums import NaclAction, ResourceType, RouteTargetKind, RuleDirection
from .resource_base import Model, NonEmptyString, Resource

Port = Field(default=None, ge=-1, le=65535)  # -1 means all ports or ICMP types


class Route(Model):
    destination: str | None = None  # CIDR, IPv6 CIDR, or prefix list ID as reported
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
    referenced_account_id: str | None = Field(default=None, pattern=r"^[0-9]{12}$")
    prefix_list_id: str | None = None


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
    resource_type: Literal[ResourceType.NETWORK_INTERFACE] = ResourceType.NETWORK_INTERFACE
    private_ip_addresses: list[PrivateIpAddress] = Field(default_factory=list)
    public_ip: str | None = None
    mac_address: str | None = None

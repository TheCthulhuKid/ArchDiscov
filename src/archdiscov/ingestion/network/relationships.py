"""Derive attachments and references as evidence-backed `attached_to` dependencies.

Each dependency starts at a stored resource, cites the exact source entry, and ends at a local,
external, or unresolved target. Nothing here concludes reachability or exposure.
"""

from collections.abc import Mapping
from typing import Any

from ...models.architecture import (
    Dependency,
    DependencyTarget,
    ExternalTarget,
    LocalTarget,
    UnresolvedTarget,
)
from ...models.enums import (
    AttachmentKind,
    AwsService,
    DependencyRelationship,
    ResourceType,
    RouteTargetKind,
)
from ...models.network import NetworkInterface, RouteTable, SecurityGroup
from ..evidence_builder import EvidenceBuilder
from ..operations import Ec2Operation as Op
from .resources import Normalized, classify_route_target

RT, RK = ResourceType, RouteTargetKind
_LOCALLY_STORED = {
    RouteTargetKind.INTERNET_GATEWAY: (
        ResourceType.INTERNET_GATEWAY,
        Op.DESCRIBE_INTERNET_GATEWAYS,
    ),
    RouteTargetKind.NETWORK_INTERFACE: (
        ResourceType.NETWORK_INTERFACE,
        Op.DESCRIBE_NETWORK_INTERFACES,
    ),
    RK.INSTANCE: (RT.EC2_INSTANCE, Op.DESCRIBE_INSTANCES),
}
_AWS_TYPES = {
    RouteTargetKind.NAT_GATEWAY: "AWS::EC2::NatGateway",
    RouteTargetKind.TRANSIT_GATEWAY: "AWS::EC2::TransitGateway",
    RouteTargetKind.VPC_PEERING: "AWS::EC2::VPCPeeringConnection",
    RouteTargetKind.VPC_ENDPOINT: "AWS::EC2::VPCEndpoint",
    RouteTargetKind.PREFIX_LIST: "AWS::EC2::PrefixList",
}
SECURITY_GROUP_TYPE = "AWS::EC2::SecurityGroup"


class RelationshipBuilder:
    def __init__(
        self,
        builder: EvidenceBuilder,
        entries: list[Normalized],
        unavailable: Mapping[Op, str],
    ) -> None:
        self.builder = builder
        self.unavailable = unavailable
        self.index = {
            (e.resource.resource_type, e.resource.identity.physical_id): e for e in entries
        }
        self.dependencies: list[Dependency] = []

    def _resolve(
        self, kind: ResourceType, op: Op, ref: str, source: Normalized, source_path: str
    ) -> DependencyTarget:
        target = self.index.get((kind, ref))
        if target is not None:
            return LocalTarget(resource_id=target.resource.id)
        if op in self.unavailable:
            reason = f"{op.value} did not complete, so {ref} could not be resolved"
        else:
            reason = f"{ref} was not present in the collected results"
        return UnresolvedTarget(reason=reason, source_path=source_path)

    def _external(
        self, identifier: str, aws_type: str | None, account_id: str | None = None
    ) -> ExternalTarget:
        return ExternalTarget(
            identifier=identifier,
            account_id=account_id,
            region=None if account_id else self.builder.region,
            service=AwsService.EC2,
            aws_resource_type=aws_type,
        )

    def _link(
        self,
        source: Normalized,
        target: DependencyTarget,
        kind: AttachmentKind,
        path: str,
        value: Any,
        **attributes: Any,
    ) -> None:
        evidence_id = self.builder.source_evidence(source.resource, source.operation, path, value)
        self._add(source, target, kind, evidence_id, attributes)

    def _add(
        self,
        source: Normalized,
        target: DependencyTarget,
        kind: AttachmentKind,
        evidence_id: str,
        attributes: dict[str, Any],
    ) -> None:
        self.dependencies.append(
            Dependency(
                source_id=source.resource.id,
                target=target,
                relationship=DependencyRelationship.ATTACHED_TO,
                attributes={
                    "kind": kind.value,
                    **{k: getattr(v, "value", v) for k, v in attributes.items() if v is not None},
                },
                evidence_ids=[evidence_id],
            )
        )

    def build(self) -> list[Dependency]:
        for entry in self.index.values():
            match entry.resource:
                case RouteTable():
                    self._route_table(entry)
                case SecurityGroup():
                    self._security_group(entry)
                case NetworkInterface():
                    self._network_interface(entry)
            if entry.resource.resource_type is ResourceType.INTERNET_GATEWAY:
                self._internet_gateway(entry)
            elif entry.resource.resource_type is ResourceType.NETWORK_ACL:
                self._network_acl(entry)
            elif entry.resource.resource_type is ResourceType.EC2_INSTANCE:
                self._members(entry, "SecurityGroups")
        return sorted(
            self.dependencies,
            key=lambda d: (d.source_id, d.evidence_ids[0], d.attributes["kind"]),
        )

    def _subnet_target(self, entry: Normalized, subnet_id: str, path: str) -> DependencyTarget:
        return self._resolve(ResourceType.SUBNET, Op.DESCRIBE_SUBNETS, subnet_id, entry, path)

    def _route_table(self, entry: Normalized) -> None:
        for i, assoc in enumerate(entry.item.get("Associations") or []):
            path = f"{entry.path}.Associations[{i}]"
            attrs = {"association_id": assoc.get("RouteTableAssociationId")}
            kind = AttachmentKind.ROUTE_TABLE_ASSOCIATION
            if assoc.get("SubnetId"):
                target = self._subnet_target(entry, assoc["SubnetId"], f"{path}.SubnetId")
                self._link(entry, target, kind, path, assoc, **attrs)
            elif assoc.get("Main") and entry.vpc_id:
                target = self._resolve(
                    ResourceType.VPC, Op.DESCRIBE_VPCS, entry.vpc_id, entry, f"{entry.path}.VpcId"
                )
                self._link(entry, target, kind, path, assoc, main=True, **attrs)
        for i, route in enumerate(entry.item.get("Routes") or []):
            self._route(entry, i, route)

    def _route(self, entry: Normalized, index: int, route: dict[str, Any]) -> None:
        kind, target_id, field = classify_route_target(route)
        if kind is RouteTargetKind.LOCAL:
            return
        path = f"{entry.path}.Routes[{index}]"
        if target_id is None:
            target: DependencyTarget = UnresolvedTarget(
                reason="route has no recognizable target", source_path=path
            )
        elif kind in _LOCALLY_STORED:
            stored, op = _LOCALLY_STORED[kind]
            target = self._resolve(stored, op, target_id, entry, f"{path}.{field}")
        else:
            target = self._external(target_id, _AWS_TYPES.get(kind))
        destination = (
            route.get("DestinationCidrBlock")
            or route.get("DestinationIpv6CidrBlock")
            or route.get("DestinationPrefixListId")
        )
        self._link(
            entry,
            target,
            AttachmentKind.ROUTE_TARGET,
            path,
            route,
            target_kind=kind,
            destination=destination,
            state=route.get("State"),
        )

    def _internet_gateway(self, entry: Normalized) -> None:
        for i, attachment in enumerate(entry.item.get("Attachments") or []):
            vpc_id = attachment.get("VpcId")
            if not vpc_id:
                continue
            path = f"{entry.path}.Attachments[{i}]"
            target = self._resolve(
                ResourceType.VPC, Op.DESCRIBE_VPCS, vpc_id, entry, f"{path}.VpcId"
            )
            self._link(
                entry,
                target,
                AttachmentKind.INTERNET_GATEWAY_ATTACHMENT,
                path,
                attachment,
                state=attachment.get("State"),
            )

    def _network_acl(self, entry: Normalized) -> None:
        for i, assoc in enumerate(entry.item.get("Associations") or []):
            if not assoc.get("SubnetId"):
                continue
            path = f"{entry.path}.Associations[{i}]"
            target = self._subnet_target(entry, assoc["SubnetId"], f"{path}.SubnetId")
            self._link(
                entry,
                target,
                AttachmentKind.NETWORK_ACL_ASSOCIATION,
                path,
                assoc,
                association_id=assoc.get("NetworkAclAssociationId"),
            )

    def _members(self, entry: Normalized, field: str) -> None:
        for i, group in enumerate(entry.item.get(field) or []):
            group_id = group.get("GroupId")
            if not group_id:
                continue
            path = f"{entry.path}.{field}[{i}]"
            target = self._resolve(
                ResourceType.SECURITY_GROUP,
                Op.DESCRIBE_SECURITY_GROUPS,
                group_id,
                entry,
                f"{path}.GroupId",
            )
            self._link(entry, target, AttachmentKind.SECURITY_GROUP_MEMBERSHIP, path, group)

    def _network_interface(self, entry: Normalized) -> None:
        attachment = entry.item.get("Attachment") or {}
        if attachment.get("InstanceId"):
            path = f"{entry.path}.Attachment"
            target = self._resolve(
                ResourceType.EC2_INSTANCE,
                Op.DESCRIBE_INSTANCES,
                attachment["InstanceId"],
                entry,
                f"{path}.InstanceId",
            )
            self._link(
                entry,
                target,
                AttachmentKind.INSTANCE_INTERFACE,
                path,
                attachment,
                device_index=attachment.get("DeviceIndex"),
                status=attachment.get("Status"),
            )
        self._members(entry, "Groups")

    def _security_group(self, entry: Normalized) -> None:
        group = entry.resource
        assert isinstance(group, SecurityGroup)
        for name, rules in (
            ("ingress_rules", group.ingress_rules),
            ("egress_rules", group.egress_rules),
        ):
            for n, rule in enumerate(rules):
                if not (rule.referenced_group_id or rule.prefix_list_id):
                    continue
                evidence_id = group.fact_evidence[f"/{name}/{n}"][0]
                source_path = self.builder.evidence[evidence_id].configuration_path
                if rule.referenced_group_id:
                    ref = rule.referenced_group_id
                    if rule.referenced_account_id not in (None, self.builder.account_id):
                        target = self._external(
                            ref, SECURITY_GROUP_TYPE, rule.referenced_account_id
                        )
                    else:
                        target = self._resolve(
                            ResourceType.SECURITY_GROUP,
                            Op.DESCRIBE_SECURITY_GROUPS,
                            ref,
                            entry,
                            source_path,
                        )
                    target_kind = None
                else:
                    target = self._external(
                        rule.prefix_list_id, _AWS_TYPES[RouteTargetKind.PREFIX_LIST]
                    )
                    target_kind = RouteTargetKind.PREFIX_LIST
                self._add(
                    entry,
                    target,
                    AttachmentKind.SECURITY_GROUP_REFERENCE,
                    evidence_id,
                    {
                        k: v
                        for k, v in {
                            "direction": rule.direction,
                            "rule_id": rule.rule_id,
                            "protocol": rule.protocol,
                            "from_port": rule.from_port,
                            "to_port": rule.to_port,
                            "target_kind": target_kind,
                        }.items()
                        if v is not None
                    },
                )


def build_dependencies(
    builder: EvidenceBuilder, entries: list[Normalized], unavailable: Mapping[Op, str]
) -> list[Dependency]:
    return RelationshipBuilder(builder, entries, unavailable).build()

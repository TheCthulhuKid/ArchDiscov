"""Derive attachments and references as evidence-backed `attached_to` dependencies.

Each dependency starts at a stored resource, cites the exact source entry, and ends at a local,
external, or unresolved target. Nothing here concludes reachability or exposure.
"""

from collections.abc import Callable, Mapping
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
from ...models.resources.network import SecurityGroup, SecurityGroupRule
from ..evidence_builder import EvidenceBuilder
from ..operations import Ec2Operation as Op
from .resources import Normalized, classify_route_target, destination

RT, RK = ResourceType, RouteTargetKind
# The resource types this collection stores, with the operation that reads each. A reference to
# any other type names a resource outside the snapshot, so it is external, never unresolved.
_COLLECTED_BY = {
    RT.VPC: Op.DESCRIBE_VPCS,
    RT.SUBNET: Op.DESCRIBE_SUBNETS,
    RT.EC2_INSTANCE: Op.DESCRIBE_INSTANCES,
    RT.INTERNET_GATEWAY: Op.DESCRIBE_INTERNET_GATEWAYS,
    RT.SECURITY_GROUP: Op.DESCRIBE_SECURITY_GROUPS,
    RT.NETWORK_INTERFACE: Op.DESCRIBE_NETWORK_INTERFACES,
}
_ROUTE_TARGET_TYPES = {
    RK.INTERNET_GATEWAY: RT.INTERNET_GATEWAY,
    RK.NETWORK_INTERFACE: RT.NETWORK_INTERFACE,
    RK.INSTANCE: RT.EC2_INSTANCE,
    RK.NAT_GATEWAY: RT.NAT_GATEWAY,
    RK.EGRESS_ONLY_INTERNET_GATEWAY: RT.EGRESS_ONLY_INTERNET_GATEWAY,
    RK.TRANSIT_GATEWAY: RT.TRANSIT_GATEWAY,
    RK.VPC_PEERING: RT.VPC_PEERING_CONNECTION,
    RK.VPC_ENDPOINT: RT.VPC_ENDPOINT,
}


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

    def _resolve(self, kind: ResourceType | None, ref: str, source_path: str) -> DependencyTarget:
        """A stored resource, else unresolved when its operation ran, else external."""
        op = _COLLECTED_BY.get(kind) if kind is not None else None
        if op is None:
            return self._external(ref, kind)
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
            handler = _HANDLERS.get(entry.resource.resource_type)
            if handler is not None:
                handler(self, entry)
        return sorted(
            self.dependencies,
            key=lambda d: (d.source_id, d.evidence_ids[0], d.attributes["kind"]),
        )

    def _subnet_target(self, subnet_id: str, path: str) -> DependencyTarget:
        return self._resolve(ResourceType.SUBNET, subnet_id, path)

    def _route_table(self, entry: Normalized) -> None:
        for i, assoc in enumerate(entry.item.get("Associations") or []):
            path = f"{entry.path}.Associations[{i}]"
            attrs = {"association_id": assoc.get("RouteTableAssociationId")}
            kind = AttachmentKind.ROUTE_TABLE_ASSOCIATION
            if assoc.get("SubnetId"):
                target = self._subnet_target(assoc["SubnetId"], f"{path}.SubnetId")
                self._link(entry, target, kind, path, assoc, **attrs)
            elif assoc.get("Main") and entry.vpc_id:
                target = self._resolve(ResourceType.VPC, entry.vpc_id, f"{entry.path}.VpcId")
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
        else:
            target = self._resolve(_ROUTE_TARGET_TYPES.get(kind), target_id, f"{path}.{field}")
        self._link(
            entry,
            target,
            AttachmentKind.ROUTE_TARGET,
            path,
            route,
            target_kind=kind,
            destination=destination(route),
            state=route.get("State"),
        )
        prefix_list = route.get("DestinationPrefixListId")
        if prefix_list:
            target = self._resolve(
                ResourceType.MANAGED_PREFIX_LIST, prefix_list, f"{path}.DestinationPrefixListId"
            )
            self._link(entry, target, AttachmentKind.PREFIX_LIST_REFERENCE, path, route)

    def _internet_gateway(self, entry: Normalized) -> None:
        for i, attachment in enumerate(entry.item.get("Attachments") or []):
            vpc_id = attachment.get("VpcId")
            if not vpc_id:
                continue
            path = f"{entry.path}.Attachments[{i}]"
            target = self._resolve(ResourceType.VPC, vpc_id, f"{path}.VpcId")
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
            target = self._subnet_target(assoc["SubnetId"], f"{path}.SubnetId")
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
            target = self._resolve(ResourceType.SECURITY_GROUP, group_id, f"{path}.GroupId")
            self._link(entry, target, AttachmentKind.SECURITY_GROUP_MEMBERSHIP, path, group)

    def _instance(self, entry: Normalized) -> None:
        self._members(entry, "SecurityGroups")

    def _network_interface(self, entry: Normalized) -> None:
        attachment = entry.item.get("Attachment") or {}
        if attachment.get("InstanceId"):
            path = f"{entry.path}.Attachment"
            target = self._resolve(
                ResourceType.EC2_INSTANCE, attachment["InstanceId"], f"{path}.InstanceId"
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
        if not isinstance(group, SecurityGroup):
            raise TypeError("a security-group entry holds a SecurityGroup")
        for name, rules in (
            ("ingress_rules", group.ingress_rules),
            ("egress_rules", group.egress_rules),
        ):
            for n, rule in enumerate(rules):
                if not (rule.referenced_group_id or rule.prefix_list_id):
                    continue
                evidence_id = group.fact_evidence[f"/{name}/{n}"][0]
                source_path = self.builder.evidence[evidence_id].configuration_path
                target, kind = self._rule_target(rule, source_path)
                self._add(
                    entry,
                    target,
                    kind,
                    evidence_id,
                    {
                        "direction": rule.direction,
                        "rule_id": rule.rule_id,
                        "protocol": rule.protocol,
                        "from_port": rule.from_port,
                        "to_port": rule.to_port,
                    },
                )

    def _rule_target(
        self, rule: SecurityGroupRule, source_path: str
    ) -> tuple[DependencyTarget, AttachmentKind]:
        """The group or prefix list a rule references, and the kind of that reference."""
        ref = rule.referenced_group_id
        if not ref:
            target = self._resolve(RT.MANAGED_PREFIX_LIST, rule.prefix_list_id, source_path)
            return target, AttachmentKind.PREFIX_LIST_REFERENCE
        if rule.referenced_account_id not in (None, self.builder.account_id):
            target = self._external(ref, RT.SECURITY_GROUP, rule.referenced_account_id)
        else:
            target = self._resolve(RT.SECURITY_GROUP, ref, source_path)
        return target, AttachmentKind.SECURITY_GROUP_REFERENCE


_HANDLERS: dict[ResourceType, Callable[[RelationshipBuilder, Normalized], None]] = {
    ResourceType.ROUTE_TABLE: RelationshipBuilder._route_table,
    ResourceType.SECURITY_GROUP: RelationshipBuilder._security_group,
    ResourceType.NETWORK_INTERFACE: RelationshipBuilder._network_interface,
    ResourceType.INTERNET_GATEWAY: RelationshipBuilder._internet_gateway,
    ResourceType.NETWORK_ACL: RelationshipBuilder._network_acl,
    ResourceType.EC2_INSTANCE: RelationshipBuilder._instance,
}


def build_dependencies(
    builder: EvidenceBuilder, entries: list[Normalized], unavailable: Mapping[Op, str]
) -> list[Dependency]:
    return RelationshipBuilder(builder, entries, unavailable).build()

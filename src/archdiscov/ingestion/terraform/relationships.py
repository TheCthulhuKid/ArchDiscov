"""Only observed references drive containment and attachment dependencies."""

from collections import defaultdict

from ...models.architecture import (
    Dependency,
    LocalTarget,
    RegionArchitecture,
    UnresolvedTarget,
)
from ...models.enums import AttachmentKind, AwsRegion, DependencyRelationship, ResourceType
from ...models.resource_base import Resource

_VPC_PARENT = ("vpc_id", ResourceType.VPC)
_SUBNET_PARENT = ("subnet_id", ResourceType.SUBNET)
_PARENT_CANDIDATES = {
    ResourceType.SUBNET: (_VPC_PARENT,),
    ResourceType.EC2_INSTANCE: (_SUBNET_PARENT, _VPC_PARENT),
    ResourceType.NETWORK_INTERFACE: (_SUBNET_PARENT, _VPC_PARENT),
    ResourceType.SECURITY_GROUP: (_VPC_PARENT,),
    ResourceType.ROUTE_TABLE: (_VPC_PARENT,),
    ResourceType.INTERNET_GATEWAY: (_VPC_PARENT,),
    ResourceType.NAT_GATEWAY: (_SUBNET_PARENT, _VPC_PARENT),
    ResourceType.EGRESS_ONLY_INTERNET_GATEWAY: (_VPC_PARENT,),
    ResourceType.VPC_ENDPOINT: (_VPC_PARENT,),
}


_REFERENCES = (
    ("vpc_id", ResourceType.VPC),
    ("subnet_id", ResourceType.SUBNET),
    ("security_group_ids", ResourceType.SECURITY_GROUP),
)
# References only some typed models hold, such as a peering connection's two VPCs. An
# endpoint's network interfaces are not references: AWS creates them, and no input declares one.
_TYPE_REFERENCES = {
    ResourceType.VPC_PEERING_CONNECTION: (
        ("requester/vpc_id", ResourceType.VPC),
        ("accepter/vpc_id", ResourceType.VPC),
    ),
    ResourceType.VPC_ENDPOINT: (
        ("route_table_ids", ResourceType.ROUTE_TABLE),
        ("subnet_ids", ResourceType.SUBNET),
    ),
    # Only VPC attachments are mapped, so the attached resource is a VPC.
    ResourceType.TRANSIT_GATEWAY_ATTACHMENT: (
        ("transit_gateway_id", ResourceType.TRANSIT_GATEWAY),
        ("attached_resource_id", ResourceType.VPC),
    ),
    ResourceType.TRANSIT_GATEWAY_ROUTE_TABLE: (
        ("transit_gateway_id", ResourceType.TRANSIT_GATEWAY),
    ),
}


def _reference(resource: Resource, field: str) -> tuple[object, str]:
    """A reference field's value and fact pointer: the typed field when the model has one."""
    head, _, nested = field.partition("/")
    if head in type(resource).model_fields:
        value = getattr(resource, head)
        return (getattr(value, nested, None) if nested else value), f"/{field}"
    return resource.attributes.get(field), f"/attributes/{field}"


def place_resources(resources: list[Resource], evidence: dict) -> tuple[dict, list[Resource], dict]:
    index = {
        (resource.identity.region, resource.identity.physical_id): resource
        for resource in resources
        if resource.identity.physical_id is not None
    }
    regions: dict[AwsRegion, RegionArchitecture] = {}
    globals_: list[Resource] = []
    dependencies: dict[AwsRegion, list[Dependency]] = defaultdict(list)
    for resource in resources:
        region = resource.identity.region
        parent = None
        # Scope is explicit configuration, not a reference inferred from module placement.
        for field, expected in _PARENT_CANDIDATES.get(resource.resource_type, ()):
            reference, _ = _reference(resource, field)
            if not isinstance(reference, str):
                continue
            candidate = index.get((region, reference))
            if (
                candidate is not None
                and candidate.resource_type == expected
                and candidate is not resource
            ):
                parent = candidate
                break
            resource.attributes.setdefault("unresolved_parent", reference)
        if parent is not None:
            parent.children.append(resource)
        elif region == AwsRegion.GLOBAL:
            globals_.append(resource)
        else:
            regions.setdefault(region, RegionArchitecture()).resources.append(resource)
        typed = _TYPE_REFERENCES.get(resource.resource_type, ())
        for field, expected in (*_REFERENCES, *typed):
            value, base = _reference(resource, field)
            references = (
                value if isinstance(value, list) else [value] if isinstance(value, str) else []
            )
            for position, reference in enumerate(references):
                pointer = base + (f"/{position}" if isinstance(value, list) else "")
                citations = resource.fact_evidence.get(pointer)
                if not citations:
                    continue
                target = index.get((region, reference))
                resolved = target is not None and target.resource_type == expected
                dependencies[region].append(
                    Dependency(
                        source_id=resource.id,
                        target=LocalTarget(resource_id=target.id)
                        if resolved
                        else UnresolvedTarget(
                            reason="Referenced resource is missing or has an unexpected type",
                            source_path=evidence[citations[0]].configuration_path,
                        ),
                        relationship=DependencyRelationship.ATTACHED_TO,
                        attributes={"kind": AttachmentKind.TERRAFORM_REFERENCE, "field": field},
                        evidence_ids=citations,
                    )
                )
    return regions, globals_, dict(dependencies)

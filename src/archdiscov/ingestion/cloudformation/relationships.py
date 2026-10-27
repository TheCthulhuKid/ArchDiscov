"""Configuration references are not proof of deployed attachments or access."""

from ...models.architecture import Dependency, LocalTarget, RegionArchitecture, UnresolvedTarget
from ...models.enums import AttachmentKind, AwsRegion, DependencyRelationship, ResourceType
from ...models.sanitization import is_sensitive_path

_PARENTS = {
    ResourceType.SUBNET: (("VpcId", ResourceType.VPC),),
    ResourceType.EC2_INSTANCE: (("SubnetId", ResourceType.SUBNET),),
    ResourceType.SECURITY_GROUP: (("VpcId", ResourceType.VPC),),
    ResourceType.ROUTE_TABLE: (("VpcId", ResourceType.VPC),),
    ResourceType.NETWORK_INTERFACE: (
        ("SubnetId", ResourceType.SUBNET),
        ("VpcId", ResourceType.VPC),
    ),
    ResourceType.NAT_GATEWAY: (
        ("SubnetId", ResourceType.SUBNET),
        ("VpcId", ResourceType.VPC),
    ),
    ResourceType.EGRESS_ONLY_INTERNET_GATEWAY: (("VpcId", ResourceType.VPC),),
    ResourceType.VPC_ENDPOINT: (("VpcId", ResourceType.VPC),),
}


def link(builder, source, path, logical_id=None, *, reason=None, attribute=None):
    if is_sensitive_path(path):
        return
    target = builder.resources.get(logical_id)
    citations = builder.cite(
        source,
        path,
        {"logical_id": logical_id, "attribute": attribute} if logical_id else None,
        reason=reason if not logical_id else None,
    )
    resolved = target is not None
    explanation = reason or "Referenced declaration is unavailable in this template"
    builder.dependencies[source.identity.region].append(
        Dependency(
            source_id=source.id,
            target=LocalTarget(resource_id=target.id)
            if resolved
            else UnresolvedTarget(reason=explanation, source_path=path),
            relationship=DependencyRelationship.ATTACHED_TO,
            attributes={"kind": AttachmentKind.CLOUDFORMATION_REFERENCE},
            evidence_ids=citations,
        )
    )
    if not resolved:
        builder.limit(source, path, explanation)


def place_resources(builder, references):
    regions, globals_ = {}, []
    for logical_id, resource in builder.resources.items():
        parent = None
        for field, expected in _PARENTS.get(resource.resource_type, ()):
            candidates = {
                ref.logical_id
                for ref in references.get(logical_id, [])
                if ref.path == f"/{field}" and not ref.attribute
            }
            if len(candidates) == 1:
                candidate = builder.resources.get(next(iter(candidates)))
                if candidate is not None and candidate.resource_type == expected:
                    parent = candidate
                    break
                resource.attributes["unresolved_parent"] = "Referenced parent is unavailable"
        if parent is not None:
            parent.children.append(resource)
        elif resource.identity.region == AwsRegion.GLOBAL:
            globals_.append(resource)
        else:
            regions.setdefault(resource.identity.region, RegionArchitecture()).resources.append(
                resource
            )
    return regions, globals_

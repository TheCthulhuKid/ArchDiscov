"""Separately declared children: resources that each declare part of a typed element.

A child, such as `aws_ec2_managed_prefix_list_entry`, stays an unknown summary and is not
folded into its parent. Its reference to the parent is cited from the child's own field. The
parent records that its list may be incomplete, because its own declaration is not the whole
list; a list the model can leave unset is unset, never partial.
"""

from collections.abc import Callable
from dataclasses import dataclass

from ...models.architecture import Dependency, LocalTarget, UnresolvedTarget
from ...models.enums import AttachmentKind, AwsRegion, DependencyRelationship, ResourceType
from ...models.resource_base import Resource


@dataclass(frozen=True)
class Child:
    field: str  # the child's attribute naming its parent
    parent_type: ResourceType
    parent_list: str  # the parent's typed list the child adds to
    unset: bool = False  # leave that list unset, since a partial list would read as whole


CHILDREN = {
    "aws_ec2_managed_prefix_list_entry": Child(
        "prefix_list_id", ResourceType.MANAGED_PREFIX_LIST, "entries", unset=True
    ),
    "aws_vpc_endpoint_route_table_association": Child(
        "vpc_endpoint_id", ResourceType.VPC_ENDPOINT, "route_table_ids"
    ),
    "aws_vpc_endpoint_subnet_association": Child(
        "vpc_endpoint_id", ResourceType.VPC_ENDPOINT, "subnet_ids"
    ),
    "aws_vpc_endpoint_security_group_association": Child(
        "vpc_endpoint_id", ResourceType.VPC_ENDPOINT, "security_group_ids"
    ),
}


@dataclass(frozen=True)
class DeclaredChild:
    resource: Resource
    child: Child
    parent_id: str  # the parent's physical ID, as the child declares it
    path: str  # the cited source path of that ID
    evidence_ids: list[str]


def link_children(
    children: list[DeclaredChild],
    resources: list[Resource],
    dependencies: dict[AwsRegion, list[Dependency]],
    limit: Callable[[Resource, str, str], None],
) -> None:
    """Reference each child's parent and qualify the parent's list it adds to."""
    parents = {(r.resource_type, r.identity.region, r.identity.physical_id): r for r in resources}
    for declared in children:
        child, region = declared.child, declared.resource.identity.region
        parent = parents.get((child.parent_type, region, declared.parent_id))
        if parent is None:
            target = UnresolvedTarget(
                reason="Referenced resource is missing or has an unexpected type",
                source_path=declared.path,
            )
        else:
            target = LocalTarget(resource_id=parent.id)
            _qualify(parent, child, declared.path, limit)
        dependencies.setdefault(region, []).append(
            Dependency(
                source_id=declared.resource.id,
                target=target,
                relationship=DependencyRelationship.ATTACHED_TO,
                attributes={"kind": AttachmentKind.TERRAFORM_REFERENCE, "field": child.field},
                evidence_ids=declared.evidence_ids,
            )
        )


def _qualify(
    parent: Resource, child: Child, path: str, limit: Callable[[Resource, str, str], None]
) -> None:
    name = child.parent_list
    if child.unset:
        setattr(parent, name, None)
        for pointer in [p for p in parent.fact_evidence if p.split("/")[1] == name]:
            del parent.fact_evidence[pointer]
    limit(parent, path, f"Its {name} may be incomplete: a separate resource declares part of it.")

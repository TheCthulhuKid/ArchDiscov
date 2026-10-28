"""Separately declared children: resources that each declare part of a typed element.

A child, such as `aws_ec2_managed_prefix_list_entry`, stays an unknown summary and is not
folded into its parent. Its references, to the parent and to anything else it names, are cited
from the child's own fields. The parent records that its list may be incomplete, because its
own declaration is not the whole list; a list the model can leave unset is unset, never partial.
"""

from collections.abc import Callable
from dataclasses import dataclass

from ...models.architecture import Dependency, LocalTarget, UnresolvedTarget
from ...models.enums import AttachmentKind, AwsRegion, DependencyRelationship, ResourceType
from ...models.resource_base import Resource
from ..import_fields import incomplete_list

RT = ResourceType


@dataclass(frozen=True)
class Child:
    field: str  # the child's attribute naming its parent
    parent_type: ResourceType
    parent_list: str  # the parent's typed list the child adds to
    unset: bool = False  # leave that list unset, since a partial list would read as whole
    # Other (attribute, type) references the child declares, such as a route's attachment.
    references: tuple[tuple[str, ResourceType], ...] = ()

    def fields(self) -> tuple[tuple[str, ResourceType], ...]:
        return ((self.field, self.parent_type), *self.references)


_ATTACHMENT = (("transit_gateway_attachment_id", RT.TRANSIT_GATEWAY_ATTACHMENT),)
CHILDREN = {
    "aws_ec2_managed_prefix_list_entry": Child(
        "prefix_list_id", RT.MANAGED_PREFIX_LIST, "entries", unset=True
    ),
    "aws_vpc_endpoint_route_table_association": Child(
        "vpc_endpoint_id", RT.VPC_ENDPOINT, "route_table_ids"
    ),
    "aws_vpc_endpoint_subnet_association": Child("vpc_endpoint_id", RT.VPC_ENDPOINT, "subnet_ids"),
    "aws_vpc_endpoint_security_group_association": Child(
        "vpc_endpoint_id", RT.VPC_ENDPOINT, "security_group_ids"
    ),
    "aws_ec2_transit_gateway_route_table_association": Child(
        "transit_gateway_route_table_id",
        RT.TRANSIT_GATEWAY_ROUTE_TABLE,
        "associations",
        references=_ATTACHMENT,
    ),
    "aws_ec2_transit_gateway_route_table_propagation": Child(
        "transit_gateway_route_table_id",
        RT.TRANSIT_GATEWAY_ROUTE_TABLE,
        "propagations",
        references=_ATTACHMENT,
    ),
    "aws_ec2_transit_gateway_route": Child(
        "transit_gateway_route_table_id",
        RT.TRANSIT_GATEWAY_ROUTE_TABLE,
        "routes",
        references=_ATTACHMENT,
    ),
}


@dataclass(frozen=True)
class DeclaredReference:
    """One reference a child declares, with its cited source."""

    resource: Resource
    child: Child
    field: str
    target_type: ResourceType
    target_id: str  # the physical ID, as the child declares it
    path: str
    evidence_ids: list[str]


def link_children(
    references: list[DeclaredReference],
    resources: list[Resource],
    dependencies: dict[AwsRegion, list[Dependency]],
    limit: Callable[[Resource, str, str], None],
) -> None:
    """Link each declared reference and qualify the parent's list the child adds to."""
    by_identity = {
        (r.resource_type, r.identity.region, r.identity.physical_id): r for r in resources
    }
    for declared in references:
        region = declared.resource.identity.region
        target = by_identity.get((declared.target_type, region, declared.target_id))
        if target is None:
            reached: LocalTarget | UnresolvedTarget = UnresolvedTarget(
                reason="Referenced resource is missing or has an unexpected type",
                source_path=declared.path,
            )
        else:
            reached = LocalTarget(resource_id=target.id)
            if declared.field == declared.child.field:
                _qualify(target, declared.child, declared.path, limit)
        dependencies.setdefault(region, []).append(
            Dependency(
                source_id=declared.resource.id,
                target=reached,
                relationship=DependencyRelationship.ATTACHED_TO,
                attributes={"kind": AttachmentKind.TERRAFORM_REFERENCE, "field": declared.field},
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
    limit(parent, path, incomplete_list(name))

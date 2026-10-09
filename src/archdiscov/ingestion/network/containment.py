"""Place normalized resources under observed parents; never invent a missing parent."""

from ...models.enums import ResourceType
from ...models.resource_base import Resource
from .resources import Normalized


def missing_parent(resource: Resource, kind: str, parent_id: str) -> None:
    resource.attributes["unresolved_parent"] = {
        "kind": kind,
        "id": parent_id,
        "reason": f"{kind} {parent_id} was not present in the collected results",
    }


def place(entries: list[Normalized]) -> list[Resource]:
    """Return regional roots: VPCs, plus resources whose parent was not collected.

    A resource goes under its subnet if collected, else its VPC, else the root. A referenced
    but uncollected parent is recorded as `unresolved_parent`.
    """
    vpcs = {
        e.resource.identity.physical_id: e
        for e in entries
        if e.resource.resource_type.name == "VPC"
    }
    subnets = {
        e.resource.identity.physical_id: e
        for e in entries
        if e.resource.resource_type.name == "SUBNET"
    }
    roots: list[Resource] = [e.resource for e in vpcs.values()]
    for entry in entries:
        if entry.resource.identity.physical_id in vpcs:
            continue
        resource = entry.resource
        if entry.subnet_id in subnets:
            subnets[entry.subnet_id].resource.children.append(resource)  # type: ignore[arg-type]
        elif entry.vpc_id in vpcs:
            vpcs[entry.vpc_id].resource.children.append(resource)  # type: ignore[arg-type]
            if entry.subnet_id:
                missing_parent(resource, "subnet", entry.subnet_id)
        else:
            if entry.subnet_id:
                missing_parent(resource, "subnet", entry.subnet_id)
            elif entry.vpc_id:
                missing_parent(resource, "vpc", entry.vpc_id)
            roots.append(resource)
    _order(roots)
    return roots


def _by_type(entries: list[Normalized], kind: ResourceType) -> dict[str | None, Normalized]:
    return {e.resource.identity.physical_id: e for e in entries if e.resource.resource_type == kind}


def _order(resources: list[Resource]) -> None:
    resources.sort(key=lambda r: (r.resource_type.value, r.id))
    for resource in resources:
        _order(resource.children)  # type: ignore[arg-type]

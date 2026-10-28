"""Normalize transit gateways, their attachments, and their route tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...models.resources.network import (
    TransitGateway,
    TransitGatewayAttachment,
    TransitGatewayLink,
    TransitGatewayRoute,
    TransitGatewayRouteTable,
)
from ..evidence_builder import tags_of
from ..operations import Ec2Operation as Op
from .resources import NetworkNormalizer, Normalized, _validated

if TYPE_CHECKING:
    from ...models.resource_base import Resource

    # Only annotated: detail_execution loads botocore, which analysis must not import.
    from ..detail_execution import DetailResponse


@dataclass(frozen=True)
class TransitGatewayTableReads:
    """One route table's detail responses; each is None when its read did not complete."""

    associations: list[DetailResponse] | None = None  # every page
    propagations: list[DetailResponse] | None = None
    routes: DetailResponse | None = None  # the one search page


def _value(item: dict[str, Any], field: str) -> Any:
    """A dotted field such as `Association.State`; None when any part is absent."""
    for part in field.split("."):
        item = item.get(part) if isinstance(item, dict) else None
    return item or None


def _facts(item: dict[str, Any], fields: dict[str, str]) -> dict[str, tuple[str, Any]]:
    facts = {
        pointer: (field, _value(item, field))
        for pointer, field in fields.items()
        if _value(item, field) is not None
    }
    if item.get("Tags"):
        facts["/attributes/tags"] = ("Tags", item["Tags"])
    return facts


def transit_gateway(net: NetworkNormalizer, index: int, item: dict[str, Any]) -> Normalized:
    path = f"TransitGateways[{index}]"
    op = Op.DESCRIBE_TRANSIT_GATEWAYS
    fields = {
        "/state": "State",
        "/owner_id": "OwnerId",
        "/association_default_route_table_id": "Options.AssociationDefaultRouteTableId",
        "/propagation_default_route_table_id": "Options.PropagationDefaultRouteTableId",
    }
    resource = net._build(
        TransitGateway,
        operation=op,
        item_path=path,
        item=item,
        physical_id=item["TransitGatewayId"],
        name=tags_of(item).get("Name"),
        attributes={"tags": tags_of(item) or None},
        facts=_facts(item, fields),
        **{pointer[1:]: _value(item, field) for pointer, field in fields.items()},
    )
    return Normalized(resource, item, path, op)


def transit_gateway_attachment(
    net: NetworkNormalizer,
    index: int,
    item: dict[str, Any],
    vpc_attachment: tuple[int, dict[str, Any]] | None,
) -> Normalized:
    """The attachment, with a VPC attachment's subnets from its own read when recorded.

    `vpc_attachment` is the attachment's `DescribeTransitGatewayVpcAttachments` entry and its
    position; without it the subnets stay unrecorded, never empty.
    """
    path = f"TransitGatewayAttachments[{index}]"
    op = Op.DESCRIBE_TRANSIT_GATEWAY_ATTACHMENTS
    subnets = _subnet_ids(vpc_attachment)
    fields = {
        "/state": "State",
        "/transit_gateway_id": "TransitGatewayId",
        "/attached_resource_type": "ResourceType",
        "/attached_resource_id": "ResourceId",
        "/attached_resource_owner_id": "ResourceOwnerId",
        "/association_state": "Association.State",
        "/associated_route_table_id": "Association.TransitGatewayRouteTableId",
    }
    resource = net._build(
        TransitGatewayAttachment,
        operation=op,
        item_path=path,
        item=item,
        physical_id=item["TransitGatewayAttachmentId"],
        name=tags_of(item).get("Name"),
        attributes={"tags": tags_of(item) or None},
        facts=_facts(item, fields),
        subnet_ids=subnets,
        **{pointer[1:]: _value(item, field) for pointer, field in fields.items()},
    )
    if subnets is not None and vpc_attachment is not None:
        _cite_subnets(net, resource, subnets, vpc_attachment[0])
    return Normalized(resource, item, path, op)


def _subnet_ids(vpc_attachment: tuple[int, dict[str, Any]] | None) -> list[str] | None:
    """The VPC attachment's subnets, or None when unread or malformed: never a partial list."""
    subnets = None if vpc_attachment is None else vpc_attachment[1].get("SubnetIds")
    if not isinstance(subnets, list) or not all(isinstance(s, str) and s for s in subnets):
        return None
    return subnets


def _cite_subnets(
    net: NetworkNormalizer, resource: Resource, subnets: list[str], position: int
) -> None:
    """Cite each subnet, or the empty list itself, to the VPC attachment read."""
    op = Op.DESCRIBE_TRANSIT_GATEWAY_VPC_ATTACHMENTS
    path = f"TransitGatewayVpcAttachments[{position}].SubnetIds"
    if not subnets:
        net.cite(resource, "/subnet_ids", op, path, [])
    for n, subnet in enumerate(subnets):
        net.cite(resource, f"/subnet_ids/{n}", op, f"{path}[{n}]", subnet)


def transit_gateway_route_table(
    net: NetworkNormalizer,
    index: int,
    item: dict[str, Any],
    reads: TransitGatewayTableReads | None,
) -> Normalized:
    """The table with what its detail reads returned; a missed read is a limitation."""
    path, limitations = f"TransitGatewayRouteTables[{index}]", []
    op = Op.DESCRIBE_TRANSIT_GATEWAY_ROUTE_TABLES
    reads = reads or TransitGatewayTableReads()
    links = {
        name: _links(path, pages, key, limitations)
        for name, pages, key in (
            ("associations", reads.associations, "Associations"),
            ("propagations", reads.propagations, "TransitGatewayRouteTablePropagations"),
        )
    }
    routes = _routes(path, reads.routes, limitations)
    complete = (
        None if reads.routes is None else not reads.routes.value.get("AdditionalRoutesAvailable")
    )
    resource = net._build(
        TransitGatewayRouteTable,
        operation=op,
        item_path=path,
        item=item,
        physical_id=item["TransitGatewayRouteTableId"],
        name=tags_of(item).get("Name"),
        attributes={"tags": tags_of(item) or None},
        facts=_facts(item, {"/state": "State", "/transit_gateway_id": "TransitGatewayId"}),
        state=item.get("State") or None,
        transit_gateway_id=item.get("TransitGatewayId") or None,
        routes_complete=complete,
        routes=[route for route, _ in routes],
        **{name: [link for link, _ in cited] for name, cited in links.items()},
    )
    for name, cited in links.items():
        for n, (_, (where, raw)) in enumerate(cited):
            net.cite(resource, f"/{name}/{n}", _DETAIL_OPERATIONS[name], where, raw)
    for n, (_, (where, raw)) in enumerate(routes):
        net.cite(resource, f"/routes/{n}", Op.SEARCH_TRANSIT_GATEWAY_ROUTES, where, raw)
    if reads.routes is not None:
        where = f"{reads.routes.path}.AdditionalRoutesAvailable"
        net.cite(
            resource, "/routes_complete", Op.SEARCH_TRANSIT_GATEWAY_ROUTES, where, not complete
        )
    net._with_limitations(resource, limitations)
    return Normalized(resource, item, path, op)


_DETAIL_OPERATIONS = {
    "associations": Op.GET_TRANSIT_GATEWAY_ROUTE_TABLE_ASSOCIATIONS,
    "propagations": Op.GET_TRANSIT_GATEWAY_ROUTE_TABLE_PROPAGATIONS,
}


def _links(
    path: str, pages: list[DetailResponse] | None, key: str, limitations: list[str]
) -> list[tuple[TransitGatewayLink, tuple[str, Any]]]:
    if pages is None:
        limitations.append(f"{path}: {key} were not recorded")
        return []
    out = []
    for page in pages:
        for i, raw in enumerate(page.value.get(key) or []):
            where = f"{page.path}.{key}[{i}]"
            link = _validated(
                TransitGatewayLink,
                where,
                limitations,
                attachment_id=raw.get("TransitGatewayAttachmentId"),
                state=raw.get("State"),
            )
            if link is not None:
                out.append((link, (where, raw)))
    return out


def _routes(
    path: str, page: DetailResponse | None, limitations: list[str]
) -> list[tuple[TransitGatewayRoute, tuple[str, Any]]]:
    if page is None:
        limitations.append(f"{path}: Routes were not recorded")
        return []
    out = []
    for i, raw in enumerate(page.value.get("Routes") or []):
        where = f"{page.path}.Routes[{i}]"
        attachments = raw.get("TransitGatewayAttachments") or []
        route = _validated(
            TransitGatewayRoute,
            where,
            limitations,
            destination=raw.get("DestinationCidrBlock") or raw.get("PrefixListId"),
            attachment_ids=[
                a["TransitGatewayAttachmentId"]
                for a in attachments
                if a.get("TransitGatewayAttachmentId")
            ],
            route_type=raw.get("Type"),
            state=raw.get("State"),
        )
        if route is not None:
            out.append((route, (where, raw)))
    return out

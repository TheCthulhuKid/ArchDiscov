"""API-shaped route tables and gateways for route-selection tests."""

from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import ArchitectureSnapshot
from tests.support.network_snapshot import build_network

LOCAL = {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"}


def route(destination="0.0.0.0/0", target="igw-1", state="active", **fields):
    """A route to a gateway ID, or `fields` such as NatGatewayId; `target` None omits it."""
    data = {"DestinationCidrBlock": destination, "State": state, **fields}
    if target is not None:
        data["GatewayId"] = target
    return data


def table(name, routes, *subnets, main=False):
    associations = [{"SubnetId": s, "RouteTableAssociationId": f"rtbassoc-{s}"} for s in subnets]
    if main:
        associations.append({"Main": True, "RouteTableAssociationId": f"rtbassoc-{name}"})
    return {
        "RouteTableId": name,
        "VpcId": "vpc-1",
        "Routes": routes,
        "Associations": associations,
    }


def gateway(name="igw-1", vpc="vpc-1", state="available"):
    return {"InternetGatewayId": name, "Attachments": [{"VpcId": vpc, "State": state}]}


def egress_only_gateway(name="eigw-1", vpc="vpc-1", state="attached"):
    return {"EgressOnlyInternetGatewayId": name, "Attachments": [{"VpcId": vpc, "State": state}]}


def build(
    tables: list[dict],
    gateways: list[dict] | None = None,
    *,
    egress_only_gateways: list[dict] | None = None,
    subnets: tuple[str, ...] = ("subnet-1",),
    status: str | None = "success",
    gateway_status: str | None = "success",
) -> ArchitectureSnapshot:
    """Tables and gateways through the real normalizer; a status of None records no coverage."""
    return build_network(
        {
            Op.DESCRIBE_ROUTE_TABLES: tables,
            Op.DESCRIBE_INTERNET_GATEWAYS: gateways or [],
            Op.DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS: egress_only_gateways or [],
        },
        subnets=subnets,
        covered={"DescribeRouteTables": status, "DescribeInternetGateways": gateway_status},
    )

from botocore.exceptions import ClientError

from archdiscov.graph import GraphIndex
from archdiscov.ingestion.aws import EC2_OPERATIONS
from archdiscov.ingestion.network.collector import (
    PREFIX_LIST_ENTRY_LIMIT,
    TRANSIT_GATEWAY_ROUTE_LIMIT,
)
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.ingestion.service_catalog import REGIONAL_OPERATIONS
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.services import collect_snapshots
from archdiscov.services.analysis_execution import run_analyzer
from tests.support.aws_fakes import ACCOUNT, make_context, request
from tests.support.network_data import (
    ATTACHED_ENI,
    FULL,
    INSTANCE,
    NAT_GATEWAY,
    PREFIX_LIST_ENTRIES,
    PRIVATE_ROUTE_TABLE,
    TRANSIT_GATEWAY_TABLE_PAGES,
    element_pages,
)

KEYS = dict(EC2_OPERATIONS)
ITEMS = {
    **FULL,
    Op.DESCRIBE_INSTANCES: [{"Instances": [INSTANCE]}],
    Op.DESCRIBE_NETWORK_INTERFACES: [ATTACHED_ENI],
    Op.DESCRIBE_ROUTE_TABLES: [*FULL[Op.DESCRIBE_ROUTE_TABLES], PRIVATE_ROUTE_TABLE],
}
DENIED = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeRouteTables")


def snake(operation: Op) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in operation.value).lstrip("_")


def pages(overrides=None):
    def factory(region):
        data = {op: [{KEYS[op]: items}] for op, items in ITEMS.items()}
        data.update(overrides or {})
        return {**element_pages(), **{snake(op): page for op, page in data.items()}}

    return factory


def collect(**kwargs):
    (snapshot,) = collect_snapshots(request(ACCOUNT), context=make_context(**kwargs))
    return snapshot


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def by_physical(snapshot):
    return {r.identity.physical_id: r for r in flatten(snapshot.regions["us-east-1"].resources)}


def test_collection_emits_schema_two_with_network_resources_and_relationships():
    deleted = {**NAT_GATEWAY, "NatGatewayId": "nat-2", "State": "deleted"}
    nat_pages = [{"NatGateways": [NAT_GATEWAY, deleted]}]
    snapshot = collect(pages=pages({Op.DESCRIBE_NAT_GATEWAYS: nat_pages}))
    assert snapshot.schema_version == "2"
    resources = by_physical(snapshot)
    kinds = {r.resource_type.value for r in resources.values()}
    assert {
        "AWS::EC2::RouteTable",
        "AWS::EC2::SecurityGroup",
        "AWS::EC2::NetworkInterface",
        "AWS::EC2::NatGateway",
        "AWS::EC2::EgressOnlyInternetGateway",
        "AWS::EC2::VPCPeeringConnection",
        "AWS::EC2::VPCEndpoint",
        "AWS::EC2::PrefixList",
        "AWS::EC2::TransitGateway",
        "AWS::EC2::TransitGatewayAttachment",
        "AWS::EC2::TransitGatewayRouteTable",
    } <= kinds
    assert {o.operation for o in snapshot.coverage.operations} == set(
        REGIONAL_OPERATIONS[AwsService.EC2]
    )
    assert {o.status for o in snapshot.coverage.operations} == {"success"}
    counts = {o.operation: o.result_count for o in snapshot.coverage.operations}
    assert counts["DescribeNatGateways"] == 2  # the deleted gateway as reported
    assert counts["GetManagedPrefixListEntries"] == 2  # one read per listed prefix list
    assert counts["SearchTransitGatewayRoutes"] == 1  # one search for the one table

    attachment = resources["tgw-attach-1"]
    assert attachment.subnet_ids == ["subnet-1"]
    cited = snapshot.evidence[attachment.fact_evidence["/subnet_ids/0"][0]]
    assert cited.configuration_path == "TransitGatewayVpcAttachments[0].SubnetIds[0]"
    (edge,) = (
        d
        for d in snapshot.dependencies_by_region[AwsRegion.US_EAST_1]
        if d.attributes["kind"] == "transit_gateway_attachment_subnet"
    )
    assert edge.evidence_ids == attachment.fact_evidence["/subnet_ids/0"]


def test_bounded_or_denied_detail_reads_keep_their_parents_unrecorded():
    def entries(PrefixListId, **params):
        if PrefixListId == "pl-s3":
            return ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "Get")
        assert params["MaxResults"] == PREFIX_LIST_ENTRY_LIMIT
        return {**PREFIX_LIST_ENTRIES[PrefixListId], "NextToken": "more"}

    def search(**params):
        assert params["MaxResults"] == TRANSIT_GATEWAY_ROUTE_LIMIT
        page = TRANSIT_GATEWAY_TABLE_PAGES["search_transit_gateway_routes"]
        return {**page, "AdditionalRoutesAvailable": True}

    def denied(**params):
        return ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "Get")

    overrides = {
        Op.GET_MANAGED_PREFIX_LIST_ENTRIES: entries,
        Op.SEARCH_TRANSIT_GATEWAY_ROUTES: search,
        Op.GET_TRANSIT_GATEWAY_ROUTE_TABLE_ASSOCIATIONS: denied,
        Op.DESCRIBE_TRANSIT_GATEWAY_VPC_ATTACHMENTS: [DENIED],
    }
    snapshot = collect(pages=pages(overrides))
    resources = by_physical(snapshot)
    status = {o.operation: o for o in snapshot.coverage.operations}

    lists = [r for r in resources.values() if r.resource_type.value == "AWS::EC2::PrefixList"]
    assert len(lists) == 2 and all(r.entries is None for r in lists)
    entries_read = status["GetManagedPrefixListEntries"]
    assert entries_read.status == "incomplete"
    assert entries_read.failures[0].code == "UnauthorizedOperation"
    assert "stopped at 100 entries and reported more" in entries_read.unresolved_values[0].reason

    table = resources["tgw-rtb-1"]
    assert table.routes_complete is False and len(table.routes) == 2
    assert table.associations == [] and len(table.propagations) == 1
    assert "Associations were not recorded" in table.attributes["limitations"][0]
    search_read = status["SearchTransitGatewayRoutes"]
    assert search_read.status == "incomplete"
    assert "stopped at 1000 routes" in search_read.unresolved_values[0].reason
    assert status["GetTransitGatewayRouteTableAssociations"].status == "failed"

    assert resources["tgw-attach-1"].subnet_ids is None  # unrecorded, never an empty list
    assert status["DescribeTransitGatewayVpcAttachments"].status == "failed"


def test_new_elements_leave_ipv4_analysis_unchanged():
    """The primitives do not decide the new elements yet, so recording them changes nothing."""
    without = {
        op: [{KEYS[op]: []}]
        for op in (
            Op.DESCRIBE_NAT_GATEWAYS,
            Op.DESCRIBE_EGRESS_ONLY_INTERNET_GATEWAYS,
            Op.DESCRIBE_VPC_PEERING_CONNECTIONS,
            Op.DESCRIBE_VPC_ENDPOINTS,
            Op.DESCRIBE_MANAGED_PREFIX_LISTS,
            Op.DESCRIBE_TRANSIT_GATEWAYS,
            Op.DESCRIBE_TRANSIT_GATEWAY_ATTACHMENTS,
            Op.DESCRIBE_TRANSIT_GATEWAY_ROUTE_TABLES,
        )
    }
    group = f"{ACCOUNT}/us-east-1/ec2/physical/sg-1"  # the interface endpoint's group
    results = []
    for factory in (pages(), pages(without)):
        index = GraphIndex(collect(pages=factory))
        results.append(
            [
                run_analyzer(index, parameters).model_dump(mode="json", exclude={"created_at"})
                for parameters in (
                    {"kind": "internet_exposure"},
                    {"kind": "security_group_impact", "resource_id": group},
                )
            ]
        )
    with_elements, without_elements = results
    assert with_elements[0]["findings"]  # the public instance is a finding
    assert with_elements == without_elements

import pytest

from archdiscov.ingestion.terraform.mapping import map_resource
from archdiscov.ingestion.terraform.relationships import place_resources
from tests.support.terraform_data import identity


def resource(kind, physical_id, **values):
    return map_resource(kind, values, identity(kind, physical_id))[0]


@pytest.mark.parametrize(
    "kind,parent_id",
    [
        ("aws_subnet", "vpc-1"),
        ("aws_instance", "subnet-1"),
        ("aws_network_interface", "subnet-1"),
        ("aws_security_group", "vpc-1"),
        ("aws_route_table", "vpc-1"),
        ("aws_internet_gateway", "vpc-1"),
    ],
)
def test_parent_candidates_keep_subnet_before_vpc_regardless_of_input_order(kind, parent_id):
    vpc = resource("aws_vpc", "vpc-1")
    subnet = resource("aws_subnet", "subnet-1", vpc_id="vpc-1")
    child = resource(kind, "child-1", subnet_id="subnet-1", vpc_id="vpc-1")
    regions, globals_, dependencies = place_resources([child, subnet, vpc], {})
    assert regions[vpc.identity.region].resources == [vpc]
    parent = subnet if parent_id == "subnet-1" else vpc
    assert any(item is child for item in parent.children)
    assert "unresolved_parent" not in child.attributes
    assert not globals_ and not dependencies


@pytest.mark.parametrize("kind", ["aws_instance", "aws_network_interface"])
@pytest.mark.parametrize("wrong_type", [False, True])
def test_missing_or_wrong_type_subnet_falls_back_to_observed_vpc(kind, wrong_type):
    vpc = resource("aws_vpc", "vpc-1")
    child = resource(kind, "child-1", subnet_id="subnet-missing", vpc_id="vpc-1")
    resources = [child, vpc]
    if wrong_type:
        resources.append(resource("aws_security_group", "subnet-missing"))
    regions, _, _ = place_resources(resources, {})
    assert any(item is vpc for item in regions[vpc.identity.region].resources)
    assert vpc.children == [child]
    assert child.attributes["unresolved_parent"] == "subnet-missing"
